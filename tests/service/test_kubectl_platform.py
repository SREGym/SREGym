import time
from unittest.mock import Mock, patch

import pytest
from kubernetes import client

from sregym.service.kubectl import ContainerPlatformError, KubeCtl, PodReadinessTimeout


def _pod(message, *, init=False, ready=False, previous=False):
    failure = client.V1ContainerState(
        waiting=client.V1ContainerStateWaiting(reason="ImagePullBackOff", message=message)
    )
    status = client.V1ContainerStatus(
        name="app",
        image="example/app:latest",
        image_id="",
        ready=ready,
        restart_count=1,
        state=client.V1ContainerState(running=client.V1ContainerStateRunning()) if previous else failure,
        last_state=failure if previous else None,
    )
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name="app-123"),
        spec=client.V1PodSpec(containers=[], node_name="arm-worker"),
        status=client.V1PodStatus(
            phase="Pending",
            init_container_statuses=[status] if init else [],
            container_statuses=[] if init else [status],
        ),
    )


@pytest.mark.parametrize("init", [False, True])
@pytest.mark.parametrize(
    "message",
    [
        "failed to start: exec format error",
        "no matching manifest for linux/arm64/v8 in the manifest list entries",
        "no match for platform in manifest: not found",
    ],
)
def test_platform_failure_is_immediate_and_identifies_image_and_node(init, message):
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.list_pods = Mock(return_value=client.V1PodList(items=[_pod(message, init=init)]))
    with (
        patch("sregym.service.kubectl.time.sleep") as sleep,
        pytest.raises(ContainerPlatformError, match="example/app:latest.*arm-worker") as error,
    ):
        kubectl.wait_for_ready("test", max_wait=10)
    sleep.assert_not_called()
    assert "Verify that the image and its executables support the node architecture." in str(error.value)


def test_transient_pull_failure_can_recover():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.list_pods = Mock(
        side_effect=[
            client.V1PodList(items=[_pod("temporary registry timeout")]),
            client.V1PodList(items=[_pod("", ready=True)]),
        ]
    )
    with patch("sregym.service.kubectl.time.sleep") as sleep:
        kubectl.wait_for_ready("test", max_wait=10)
    sleep.assert_called_once()


def test_preload_image_creates_all_node_pods_before_waiting_and_cleans_up():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.list_node.return_value = client.V1NodeList(
        items=[
            client.V1Node(metadata=client.V1ObjectMeta(name="worker-a")),
            client.V1Node(metadata=client.V1ObjectMeta(name="worker-b")),
        ]
    )
    kubectl.core_v1_api.read_namespaced_pod.return_value = client.V1Pod(
        metadata=client.V1ObjectMeta(name="preload"),
        spec=client.V1PodSpec(containers=[]),
        status=client.V1PodStatus(phase="Succeeded"),
    )

    kubectl.preload_image_on_nodes("registry.example/helper:v1", "openebs", timeout=10)

    creates = kubectl.core_v1_api.create_namespaced_pod.call_args_list
    assert len(creates) == 2
    assert {call.kwargs["body"].spec.node_name for call in creates} == {"worker-a", "worker-b"}
    assert all(call.kwargs["body"].spec.containers[0].image == "registry.example/helper:v1" for call in creates)
    assert len({call.kwargs["body"].metadata.name for call in creates}) == 2
    assert kubectl.core_v1_api.method_calls[1:3] == [
        call for call in kubectl.core_v1_api.method_calls if call[0] == "create_namespaced_pod"
    ]
    assert kubectl.core_v1_api.delete_namespaced_pod.call_count == 2
    for method, _args, kwargs in kubectl.core_v1_api.method_calls:
        budget = kwargs["_request_timeout"]
        assert all(0 < value <= 10 for value in (budget if isinstance(budget, tuple) else (budget,))), method


def test_preload_accounts_for_a_create_that_times_out_after_reaching_the_api():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.list_node.return_value = client.V1NodeList(
        items=[client.V1Node(metadata=client.V1ObjectMeta(name="worker-a"))]
    )
    kubectl.core_v1_api.create_namespaced_pod.side_effect = TimeoutError("API did not acknowledge creation")
    kubectl.core_v1_api.delete_namespaced_pod.side_effect = TimeoutError("API unavailable during cleanup")

    with pytest.raises(TimeoutError, match="API did not acknowledge creation"):
        kubectl.preload_image_on_nodes("registry.example/helper:v1", "openebs", timeout=10)

    name = kubectl.core_v1_api.create_namespaced_pod.call_args.kwargs["body"].metadata.name
    kubectl.core_v1_api.delete_namespaced_pod.assert_called_once_with(
        name=name,
        namespace="openebs",
        grace_period_seconds=0,
        _request_timeout=10,
    )


def test_preload_node_lookup_uses_the_callers_deadline():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.list_node.side_effect = TimeoutError("node API timed out")

    with pytest.raises(TimeoutError, match="node API timed out"):
        kubectl.preload_image_on_nodes("registry.example/helper:v1", "openebs", timeout=0.25)

    assert all(0 < value <= 0.25 for value in kubectl.core_v1_api.list_node.call_args.kwargs["_request_timeout"])
    kubectl.core_v1_api.create_namespaced_pod.assert_not_called()


def test_preload_deadline_reaches_the_real_kubernetes_http_transport():
    kubectl = KubeCtl.__new__(KubeCtl)
    api = client.ApiClient(client.Configuration())
    request = Mock(return_value=Mock(status=200, data=b'{"apiVersion":"v1","kind":"NodeList","items":[]}'))
    api.rest_client.pool_manager.request = request
    kubectl.core_v1_api = client.CoreV1Api(api)

    with pytest.raises(RuntimeError, match="cluster has no nodes"):
        kubectl.preload_image_on_nodes("registry.example/helper:v1", "openebs", timeout=0.25)

    # Kubernetes 30.1 silently ignores scalar floats before calling urllib3.
    timeout = request.call_args.kwargs["timeout"]
    assert 0 < timeout.connect_timeout <= 0.25
    assert 0 < timeout.read_timeout <= 0.25


def test_readiness_timeout_names_the_blocking_container_and_pull_error(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.list_pods = Mock(return_value=client.V1PodList(items=[_pod("temporary registry timeout")]))
    kubectl.core_v1_api = Mock()

    with pytest.raises(PodReadinessTimeout, match="app-123.*ImagePullBackOff.*temporary registry timeout"):
        kubectl.wait_for_ready("test", sleep=1, max_wait=1)


def test_platform_status_is_checked_before_slow_logs_from_other_pods():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.list_pods = Mock(return_value=client.V1PodList(items=[_failed_pod(), _pod("exec format error")]))
    kubectl.core_v1_api = Mock()
    with pytest.raises(ContainerPlatformError):
        kubectl.wait_for_ready("test", max_wait=5)
    kubectl.core_v1_api.read_namespaced_pod_log.assert_not_called()


def test_ready_container_does_not_fail_on_stale_platform_error():
    KubeCtl._check_container_platform(_pod("exec format error", ready=True, previous=True), "test")


def test_previous_failure_detected_while_container_still_unready():
    with pytest.raises(ContainerPlatformError):
        KubeCtl._check_container_platform(_pod("exec format error", previous=True), "test")


def test_terminated_container_start_error_is_detected():
    pod = _pod("")
    pod.status.container_statuses[0].state = client.V1ContainerState(
        terminated=client.V1ContainerStateTerminated(
            exit_code=128, reason="StartError", message="failed to start container: exec format error"
        )
    )
    with pytest.raises(ContainerPlatformError):
        KubeCtl._check_container_platform(pod, "test")


def _failed_pod(*, init=False, exit_code=1):
    pod = _pod("", init=init)
    statuses = pod.status.init_container_statuses if init else pod.status.container_statuses
    statuses[0].state = client.V1ContainerState(
        terminated=client.V1ContainerStateTerminated(exit_code=exit_code, reason="Error")
    )
    return pod


@pytest.mark.parametrize("init", [False, True])
@pytest.mark.parametrize(
    "logs",
    [
        "exec /bin/server: exec format error\n",
        "This usually means that you're running an x86 program on an arm64 OS without multi-arch libraries.\n",
    ],
)
def test_failed_container_log_identifies_platform_when_status_has_no_message(init, logs):
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.list_pods = Mock(return_value=client.V1PodList(items=[_failed_pod(init=init)]))
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.read_namespaced_pod_log.return_value = logs
    with (
        patch("sregym.service.kubectl.time.sleep") as sleep,
        pytest.raises(ContainerPlatformError, match="example/app:latest.*arm-worker"),
    ):
        kubectl.wait_for_ready("test", max_wait=10)
    sleep.assert_not_called()
    kubectl.core_v1_api.read_namespaced_pod_log.assert_called_once_with(
        name="app-123",
        namespace="test",
        container="app",
        previous=False,
        tail_lines=20,
        limit_bytes=4096,
        _request_timeout=3,
    )


def test_ordinary_crash_log_is_checked_only_once_per_container_instance():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.read_namespaced_pod_log.return_value = "Connection refused\n"
    pod = _failed_pod()
    checked = {}
    kubectl._check_container_platform_logs(pod, "test", checked, deadline=time.monotonic() + 10)
    kubectl._check_container_platform_logs(pod, "test", checked, deadline=time.monotonic() + 10)
    kubectl.core_v1_api.read_namespaced_pod_log.assert_called_once()


def test_log_fetch_failure_does_not_become_a_platform_error():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.read_namespaced_pod_log.side_effect = RuntimeError("log file not yet available")
    kubectl._check_container_platform_logs(_failed_pod(), "test", {}, deadline=time.monotonic() + 10)


def test_successful_init_container_does_not_trigger_log_inspection():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl._check_container_platform_logs(
        _failed_pod(init=True, exit_code=0), "test", {}, deadline=time.monotonic() + 10
    )
    kubectl.core_v1_api.read_namespaced_pod_log.assert_not_called()


def test_restarting_container_uses_previous_logs():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.read_namespaced_pod_log.return_value = "Connection refused\n"
    pod = _failed_pod()
    status = pod.status.container_statuses[0]
    status.last_state = status.state
    status.state = client.V1ContainerState(running=client.V1ContainerStateRunning())
    kubectl._check_container_platform_logs(pod, "test", {}, deadline=time.monotonic() + 10)
    assert kubectl.core_v1_api.read_namespaced_pod_log.call_args.kwargs["previous"] is True


def test_slow_log_requests_share_the_readiness_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    kubectl = KubeCtl.__new__(KubeCtl)
    pods = [_failed_pod() for _ in range(10)]
    for i, pod in enumerate(pods):
        pod.metadata.uid = f"pod-{i}"
    kubectl.list_pods = Mock(return_value=client.V1PodList(items=pods))
    kubectl.core_v1_api = Mock()

    def unavailable(**kwargs):
        now[0] += kwargs["_request_timeout"]
        raise TimeoutError("kubelet did not respond")

    kubectl.core_v1_api.read_namespaced_pod_log.side_effect = unavailable
    with pytest.raises(Exception, match="within 5 seconds"):
        kubectl.wait_for_ready("test", sleep=2, max_wait=5)
    assert now[0] == 5
    assert kubectl.core_v1_api.read_namespaced_pod_log.call_count == 2
    assert kubectl.list_pods.call_args.kwargs["timeout"] <= 5


def test_unavailable_logs_retry_after_backoff_and_can_report_platform_failure(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.read_namespaced_pod_log.side_effect = [RuntimeError("not yet available"), "exec format error"]
    pod, retry_at = _failed_pod(), {}
    kubectl._check_container_platform_logs(pod, "test", retry_at, deadline=10)
    now[0] = 4
    kubectl._check_container_platform_logs(pod, "test", retry_at, deadline=10)
    assert kubectl.core_v1_api.read_namespaced_pod_log.call_count == 1
    now[0] = 5
    with pytest.raises(ContainerPlatformError):
        kubectl._check_container_platform_logs(pod, "test", retry_at, deadline=10)
