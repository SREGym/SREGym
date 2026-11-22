"""Deadline, request-budget, and retry behavior of Kubernetes status waits."""

import contextlib
import time
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.service.kubectl import KubeCtl


@pytest.fixture
def kubectl(monkeypatch):
    subject = KubeCtl.__new__(KubeCtl)
    subject.core_v1_api = Mock()
    subject.core_v1_api.read_namespace.return_value = client.V1Namespace(metadata=client.V1ObjectMeta(name="ns"))
    subject.core_v1_api.list_namespaced_pod.return_value = client.V1PodList(items=[_pod(ready=False)])
    monkeypatch.setattr(subject, "_polling_core_v1_api", lambda: contextlib.nullcontext(subject.core_v1_api))
    return subject


def _fake_clock(monkeypatch, *, tick):
    """Replace time.monotonic and time.sleep with a controlled clock.

    sleep advances the clock by at least ``tick`` so a sleep=0 loop still
    makes progress toward the deadline.
    """
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + max(seconds, tick)))
    return now


def _pod(*, ready):
    status = client.V1ContainerStatus(
        name="app",
        image="example/app:latest",
        image_id="",
        ready=ready,
        restart_count=0,
        state=None if ready else client.V1ContainerState(waiting=client.V1ContainerStateWaiting(reason="Pending")),
    )
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name="app-123"),
        status=client.V1PodStatus(phase="Running", container_statuses=[status], conditions=[]),
    )


def test_namespace_deletion_sleep_zero_still_terminates(monkeypatch, kubectl):
    _fake_clock(monkeypatch, tick=0.05)

    with pytest.raises(Exception, match="Timeout"):
        kubectl.wait_for_namespace_deletion("ns", sleep=0, max_wait=1)

    assert kubectl.core_v1_api.read_namespace.call_count > 1


def test_namespace_deletion_returns_once_the_namespace_is_gone(monkeypatch, kubectl):
    _fake_clock(monkeypatch, tick=0.0)
    kubectl.core_v1_api.read_namespace.side_effect = ApiException(status=404, reason="Not Found")

    kubectl.wait_for_namespace_deletion("ns", sleep=2, max_wait=600)

    assert kubectl.core_v1_api.read_namespace.call_count == 1


def test_stable_sleep_zero_still_terminates(monkeypatch, kubectl):
    _fake_clock(monkeypatch, tick=0.05)

    with pytest.raises(Exception, match="Timeout"):
        kubectl.wait_for_stable("ns", sleep=0, max_wait=1)

    assert kubectl.core_v1_api.list_namespaced_pod.call_count > 1


def test_stable_returns_once_every_pod_is_ready(monkeypatch, kubectl):
    _fake_clock(monkeypatch, tick=0.0)
    kubectl.core_v1_api.list_namespaced_pod.return_value = client.V1PodList(items=[_pod(ready=True)])

    kubectl.wait_for_stable("ns", sleep=2, max_wait=600)

    assert kubectl.core_v1_api.list_namespaced_pod.call_count == 1


@pytest.fixture(params=["wait_for_namespace_deletion", "wait_for_stable"])
def wait_case(request, kubectl):
    method = getattr(kubectl, request.param)
    if request.param == "wait_for_namespace_deletion":
        api_call = kubectl.core_v1_api.read_namespace
        success = ApiException(status=404)
    else:
        api_call = kubectl.core_v1_api.list_namespaced_pod
        success = client.V1PodList(items=[_pod(ready=True)])
    return method, api_call, success


@pytest.mark.parametrize("budget", [0.15, 6, 30, 300])
def test_status_request_budget_is_capped_and_reaches_api(monkeypatch, wait_case, budget):
    _fake_clock(monkeypatch, tick=0)
    method, api_call, success = wait_case
    api_call.side_effect = [success]

    method("ns", max_wait=budget)

    connect, read = api_call.call_args.kwargs["_request_timeout"]
    assert 0 < connect <= 3
    assert connect + read == pytest.approx(min(30, budget))


@pytest.mark.parametrize("budget", [0, -1])
def test_expired_deadline_does_not_issue_a_request(monkeypatch, wait_case, budget):
    _fake_clock(monkeypatch, tick=0)
    method, api_call, _ = wait_case
    with pytest.raises(Exception, match="Timeout"):
        method("ns", max_wait=budget)
    api_call.assert_not_called()


def test_polling_retries_transient_errors_within_the_overall_deadline(monkeypatch, wait_case):
    now = _fake_clock(monkeypatch, tick=0)
    method, api_call, success = wait_case
    api_call.side_effect = [ApiException(status=503), TimeoutError("slow request"), success]

    method("ns", sleep=2, max_wait=6)

    assert now[0] == 4
    assert api_call.call_count == 3
    budgets = [sum(call.kwargs["_request_timeout"]) for call in api_call.call_args_list]
    assert budgets == [6, 4, 2]


def test_api_time_counts_and_success_after_deadline_is_rejected(monkeypatch, wait_case):
    now = _fake_clock(monkeypatch, tick=0)
    method, api_call, success = wait_case

    def late_reply(*args, **kwargs):
        now[0] += 1.1
        if isinstance(success, Exception):
            raise success
        return success

    api_call.side_effect = late_reply
    with pytest.raises(Exception, match="Timeout"):
        method("ns", sleep=0, max_wait=1)
    assert api_call.call_count == 1


def test_polling_sleep_cannot_exceed_remaining_budget(monkeypatch, wait_case):
    now = _fake_clock(monkeypatch, tick=0)
    method, api_call, _ = wait_case
    with pytest.raises(Exception, match="Timeout"):
        method("ns", sleep=2, max_wait=0.25)
    assert now[0] == 0.25
    assert api_call.call_count == 1


def test_an_empty_namespace_is_not_considered_stable(monkeypatch, kubectl):
    _fake_clock(monkeypatch, tick=0)
    kubectl.core_v1_api.list_namespaced_pod.return_value = client.V1PodList(items=[])
    with pytest.raises(Exception, match="did not settle"):
        kubectl.wait_for_stable("ns", sleep=1, max_wait=1)


def test_polling_client_preserves_configuration_and_closes_only_its_own_pool():
    configuration = client.Configuration()
    configuration.host = "https://cluster.example:6443"
    configuration.retries = 3
    configuration.api_key["authorization"] = "test-token"
    refresh = Mock()
    configuration.refresh_api_key_hook = refresh
    subject = KubeCtl.__new__(KubeCtl)
    with client.ApiClient(configuration, cookie="test-cookie") as original:
        original.default_headers["X-Cluster"] = "configured-cluster"
        subject.core_v1_api = client.CoreV1Api(original)
        with subject._polling_core_v1_api() as api:
            polling = api.api_client
            assert polling is not original
            assert polling.configuration.host == configuration.host
            assert polling.configuration.verify_ssl == configuration.verify_ssl
            assert polling.configuration.retries == 0
            assert polling.cookie == original.cookie
            assert polling.default_headers == original.default_headers
            assert polling.configuration.get_api_key_with_prefix("authorization") == "test-token"
            refresh.assert_called_once_with(polling.configuration)
            polling.rest_client.pool_manager.clear = Mock()
        polling.rest_client.pool_manager.clear.assert_called()
        assert subject.core_v1_api.api_client is original
        assert configuration.retries == 3


def test_polling_client_closes_its_pool_when_the_wait_raises():
    subject = KubeCtl.__new__(KubeCtl)
    with client.ApiClient(client.Configuration()) as original:
        subject.core_v1_api = client.CoreV1Api(original)
        with pytest.raises(RuntimeError, match="probe failed"), subject._polling_core_v1_api() as api:
            pool = api.api_client.rest_client.pool_manager
            pool.clear = Mock()
            raise RuntimeError("probe failed")
        pool.clear.assert_called()
