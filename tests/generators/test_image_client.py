"""astronomy_shop_ad_service_image_slow_load needs a client that sends the image delay header."""

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from sregym.generators.workload import image_client, image_client_worker
from sregym.generators.workload.image_client import ImageEvidenceError, image_client_deployment
from sregym.service.agent_visibility_policy import HIDDEN_NAMESPACES, is_hidden_resource
from sregym.service.apps import astronomy_shop
from sregym.service.apps.astronomy_shop import AstronomyShop


class _Response(io.BytesIO):
    def __init__(self, payload, content_type="application/json"):
        super().__init__(payload)
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = content_type


@pytest.mark.parametrize("delay", [0, 10000])
def test_worker_sends_the_valid_flag_value_and_checks_product_images(monkeypatch, delay):
    def fetch(request, timeout):
        url = request.full_url
        if url == "http://flagd:8016/ofrep/v1/evaluate/flags/imageSlowLoad":
            assert request.get_method() == "POST"
            assert json.loads(request.data) == {"context": {}}
            return _Response(json.dumps({"value": delay}).encode())
        if url == "http://frontend:8080/api/products":
            return _Response(b'[{"picture": "star.jpg"}, {"picture": "moon.jpg"}]')
        assert url in ("http://frontend:8080/images/products/star.jpg", "http://frontend:8080/images/products/moon.jpg")
        assert request.get_header("X-envoy-fault-delay-request") == str(delay)
        return _Response(b"image data", "image/jpeg")

    monkeypatch.setattr(image_client_worker, "urlopen", fetch)
    with ThreadPoolExecutor() as pool:
        sample = image_client_worker.fetch_batch("http://frontend:8080", "http://flagd:8016", "imageSlowLoad", pool)
    assert sample["flag_delay_ms"] == delay
    assert len(sample["durations_s"]) == 2


@pytest.mark.parametrize("flag_body", [b"[]", b"{}", b'{"value":"0"}', b'{"value":true}', b"invalid JSON"])
def test_worker_does_not_turn_a_bad_flag_response_into_a_zero_delay(monkeypatch, flag_body):
    fetch = Mock(side_effect=lambda *args, **kwargs: _Response(flag_body))
    monkeypatch.setattr(image_client_worker, "urlopen", fetch)
    with ThreadPoolExecutor() as pool, pytest.raises(ValueError):
        image_client_worker.fetch_batch("http://frontend:8080", "http://flagd:8016", "imageSlowLoad", pool)
    assert fetch.call_count == 1


@pytest.mark.parametrize(
    ("data", "content_type", "message"),
    [(b"", "image/jpeg", "empty image"), (b"<html>ok</html>", "text/html", "expected an image")],
)
def test_worker_rejects_a_success_status_without_image_content(monkeypatch, data, content_type, message):
    monkeypatch.setattr(image_client_worker, "urlopen", lambda *args, **kwargs: _Response(data, content_type))
    with pytest.raises(ValueError, match=message):
        image_client_worker._fetch_image("http://frontend", "star.jpg", 0)


def test_deployment_runs_the_stdlib_worker():
    deployment = image_client_deployment("astronomy-shop")
    assert deployment.metadata.name == "frontend-image-client"
    container = deployment.spec.template.spec.containers[0]
    assert container.image == "python:3.12-slim"
    assert container.command[:3] == ["python", "-u", "-c"]
    assert container.command[-3:] == ["astronomy-shop", "imageSlowLoad", "5"]
    compile(container.command[3], "image_client_worker.py", "exec")
    assert "cpu" not in container.resources.limits


def test_deployment_stops_without_waiting_for_sigterm():
    assert image_client_deployment("astronomy-shop").spec.template.spec.termination_grace_period_seconds == 0


def test_client_runs_in_a_namespace_the_agent_cannot_reach():
    # Its command names the flag, and deleting it would clear the alert without a fix.
    # A hidden namespace also blocks writes and hides its metrics and logs; a hidden label does not.
    deployment = client.ApiClient().sanitize_for_serialization(image_client_deployment("astronomy-shop"))
    assert deployment["metadata"]["namespace"] in HIDDEN_NAMESPACES
    pod = {"kind": "Pod", "metadata": {"namespace": deployment["metadata"]["namespace"]}}
    for resource in (deployment, pod):
        assert is_hidden_resource(resource, HIDDEN_NAMESPACES, hidden_labels={})


def _api_error(status):
    return client.exceptions.ApiException(status=status)


@pytest.mark.parametrize("existing", [False, True])
def test_start_creates_the_hidden_namespace_and_the_client(monkeypatch, existing):
    core, apps = Mock(), Mock()
    if existing:
        core.create_namespace.side_effect = _api_error(409)
        apps.create_namespaced_deployment.side_effect = _api_error(409)
    monkeypatch.setattr(image_client.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(image_client.client, "AppsV1Api", lambda: apps)
    image_client.start_image_client("astronomy-shop")
    assert core.create_namespace.call_args.kwargs["body"].metadata.name == image_client.IMAGE_CLIENT_NAMESPACE
    created = apps.create_namespaced_deployment.call_args.kwargs
    assert created["namespace"] == created["body"].metadata.namespace == image_client.IMAGE_CLIENT_NAMESPACE
    assert apps.replace_namespaced_deployment.called is existing


def test_stop_deletes_only_the_client_and_waits_for_its_pods():
    kubectl = Mock()
    image_client.stop_image_client(kubectl)
    kubectl.exec_command_checked.assert_called_once_with(
        "kubectl delete deployment/frontend-image-client -n khaos"
        " --ignore-not-found --cascade=foreground --wait=true --timeout=30s",
        timeout=35,
    )


def _shop(monkeypatch, enabled):
    shop = AstronomyShop.__new__(AstronomyShop)
    shop.namespace = "astronomy-shop"
    shop.helm_configs = {"namespace": "astronomy-shop"}
    shop.kubectl = Mock()
    shop.wrk = Mock()
    shop.logger = logging.getLogger(__name__)
    if enabled:
        shop.image_client_enabled = True
    calls = []
    monkeypatch.setattr(astronomy_shop, "Helm", Mock())
    monkeypatch.setattr(astronomy_shop, "start_image_client", lambda ns: calls.append(("start", ns)))
    monkeypatch.setattr(astronomy_shop, "stop_image_client", lambda kubectl: calls.append(("stop",)))
    monkeypatch.setattr(astronomy_shop, "wait_for_images", lambda *args, **kwargs: calls.append(("wait",)))
    return shop, calls


def test_workload_runs_the_client_only_when_enabled(monkeypatch):
    shop, calls = _shop(monkeypatch, enabled=False)
    shop.start_workload()
    shop.stop_workload()
    assert calls == []

    shop, calls = _shop(monkeypatch, enabled=True)
    shop.start_workload()
    shop.stop_workload()
    assert calls == [("start", "astronomy-shop"), ("wait",), ("stop",)]
    shop.wrk.start.assert_called_once()


@pytest.mark.parametrize("teardown", ["cleanup", "delete"])
@pytest.mark.parametrize("enabled", [False, True])
def test_teardown_removes_leftover_client_even_for_unrelated_problem(monkeypatch, teardown, enabled):
    shop, calls = _shop(monkeypatch, enabled=enabled)
    getattr(shop, teardown)()
    assert calls == [("stop",)]
    shop.kubectl.delete_namespace.assert_called_once_with("astronomy-shop")


def test_cleanup_still_removes_app_when_image_client_teardown_fails(monkeypatch):
    shop, _ = _shop(monkeypatch, enabled=False)
    monkeypatch.setattr(
        astronomy_shop, "stop_image_client", Mock(side_effect=RuntimeError("client deletion timed out"))
    )
    with pytest.raises(RuntimeError, match="client deletion timed out"):
        shop.cleanup()
    shop.kubectl.delete_namespace.assert_called_once_with("astronomy-shop")


def test_enabled_app_fails_setup_when_its_image_client_cannot_be_verified(monkeypatch):
    shop, _ = _shop(monkeypatch, enabled=True)
    monkeypatch.setattr(
        astronomy_shop, "wait_for_images", Mock(side_effect=ImageEvidenceError("image_evidence_missing", "no samples"))
    )
    with pytest.raises(ImageEvidenceError, match="no samples"):
        shop.start_workload()


@pytest.mark.parametrize(
    ("rows", "delayed", "alert", "reason"),
    [
        ([(90, [0.1]), (95, [0.2])], False, None, None),
        ([(90, [10.0]), (95, [10.0])], False, None, "fault_still_present"),
        ([(90, [10.0]), (95, [10.0])], True, False, "image_latency_alert_missing"),
        ([(90, [10.0]), (95, [10.0])], True, True, None),
        ([(90, [0.1]), (95, [0.1])], True, True, "image_latency_outside_target"),
        ([(20, [0.1]), (30, [0.1])], False, None, "image_evidence_missing"),
        ([(95, [0.1]), (95, [0.1])], False, None, "image_evidence_missing"),
        ([(90, [0.1]), (95, None)], False, None, "image_request_failed"),
        ([(85, [0.1]), (90, None), (95, [0.1])], False, None, "image_evidence_missing"),
        ([], False, None, "image_evidence_missing"),
    ],
)
def test_wait_requires_two_consecutive_fresh_batches_and_the_requested_alert(monkeypatch, rows, delayed, alert, reason):
    monkeypatch.setattr(image_client, "time", SimpleNamespace(time=lambda: 100, monotonic=lambda: 100))
    kubectl = Mock()
    kubectl.exec_command_checked.return_value = "\n".join(
        json.dumps({"started_at": stamp, "durations_s": durations, "error": "image request failed"})
        for stamp, durations in rows
    )
    kwargs = {
        "after": 85,
        "timeout": 0,
        "delayed": delayed,
        "alert_is_firing": None if alert is None else lambda: alert,
    }
    if reason:
        with pytest.raises(ImageEvidenceError) as exc:
            image_client.wait_for_images(kubectl, **kwargs)
        assert exc.value.reason == reason
    else:
        assert image_client.wait_for_images(kubectl, **kwargs).startswith("2 image responses")


def test_wait_rejects_unavailable_logs():
    kubectl = Mock()
    kubectl.exec_command_checked.side_effect = RuntimeError("deployment not found")
    with pytest.raises(ImageEvidenceError, match="deployment not found") as exc:
        image_client.wait_for_images(kubectl, after=0, timeout=0)
    assert exc.value.reason == "image_measurement_unavailable"


def test_image_slow_load_enables_the_client(monkeypatch):
    from sregym.conductor.problems import image_slow_load

    def app():
        shop = AstronomyShop.__new__(AstronomyShop)
        shop.namespace = "astronomy-shop"
        return shop

    monkeypatch.setattr(image_slow_load, "AstronomyShop", app)
    monkeypatch.setattr(image_slow_load, "KubeCtl", lambda: None)
    monkeypatch.setattr(image_slow_load, "OtelFaultInjector", lambda namespace: None)
    monkeypatch.setattr(image_slow_load, "LLMAsAJudgeOracle", lambda **kwargs: None)
    monkeypatch.setattr(image_slow_load, "AlertOracle", lambda **kwargs: None)
    assert image_slow_load.ImageSlowLoad().app.image_client_enabled is True
    assert AstronomyShop.image_client_enabled is False
