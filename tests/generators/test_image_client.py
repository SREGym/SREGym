"""astronomy_shop_ad_service_image_slow_load needs a client that sends the image delay header."""

import subprocess
from unittest.mock import Mock

import pytest
from kubernetes import client

from sregym.generators.workload import image_client
from sregym.generators.workload.image_client import image_client_deployment, image_client_script
from sregym.service.agent_visibility_policy import HIDDEN_NAMESPACES, is_hidden_resource
from sregym.service.apps import astronomy_shop
from sregym.service.apps.astronomy_shop import AstronomyShop


def test_script_reads_the_flag_and_sends_it_as_the_envoy_delay_header():
    script = image_client_script("astronomy-shop")
    assert "http://flagd.astronomy-shop.svc.cluster.local:8016/ofrep/v1/evaluate/flags/imageSlowLoad" in script
    assert "http://frontend-proxy.astronomy-shop.svc.cluster.local:8080/api/products" in script
    assert "x-envoy-fault-delay-request: ${delay:-0}" in script
    assert "http://frontend-proxy.astronomy-shop.svc.cluster.local:8080/images/products/$picture" in script
    subprocess.run(["sh", "-n", "-c", script], check=True)


def test_deployment_pins_the_curl_image():
    deployment = image_client_deployment("astronomy-shop")
    assert deployment.metadata.name == "frontend-image-client"
    container = deployment.spec.template.spec.containers[0]
    assert container.image == "curlimages/curl:8.10.1"
    assert "cpu" not in container.resources.limits


def test_deployment_stops_without_waiting_for_sigterm():
    # sh as PID 1 ignores SIGTERM; a grace period would only stall deletion.
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


def test_stop_deletes_only_the_client_and_ignores_a_missing_one(monkeypatch):
    apps = Mock()
    apps.delete_namespaced_deployment.side_effect = _api_error(404)
    monkeypatch.setattr(image_client.client, "AppsV1Api", lambda: apps)
    image_client.stop_image_client()
    apps.delete_namespaced_deployment.assert_called_once_with(
        name=image_client.IMAGE_CLIENT_NAME, namespace=image_client.IMAGE_CLIENT_NAMESPACE
    )


def _shop(monkeypatch, enabled):
    shop = AstronomyShop.__new__(AstronomyShop)
    shop.namespace = "astronomy-shop"
    shop.helm_configs = {"namespace": "astronomy-shop"}
    shop.kubectl = Mock()
    shop.wrk = Mock()
    if enabled:
        shop.image_client_enabled = True
    calls = []
    monkeypatch.setattr(astronomy_shop, "Helm", Mock())
    monkeypatch.setattr(astronomy_shop, "start_image_client", lambda ns: calls.append(("start", ns)))
    monkeypatch.setattr(astronomy_shop, "stop_image_client", lambda: calls.append(("stop",)))
    return shop, calls


def test_workload_runs_the_client_only_when_enabled(monkeypatch):
    shop, calls = _shop(monkeypatch, enabled=False)
    shop.start_workload()
    shop.stop_workload()
    assert calls == []

    shop, calls = _shop(monkeypatch, enabled=True)
    shop.start_workload()
    shop.stop_workload()
    assert calls == [("start", "astronomy-shop"), ("stop",)]
    shop.wrk.start.assert_called_once()


@pytest.mark.parametrize("teardown", ["cleanup", "delete"])
def test_teardown_removes_the_client_outside_the_app_namespace(monkeypatch, teardown):
    shop, calls = _shop(monkeypatch, enabled=False)
    getattr(shop, teardown)()
    assert calls == []

    shop, calls = _shop(monkeypatch, enabled=True)
    getattr(shop, teardown)()
    assert calls == [("stop",)]
    shop.kubectl.delete_namespace.assert_called_once_with("astronomy-shop")


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
