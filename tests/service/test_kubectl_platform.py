from unittest.mock import Mock, patch

import pytest
from kubernetes import client

from sregym.service.kubectl import ContainerPlatformError, KubeCtl


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
        pytest.raises(ContainerPlatformError, match="example/app:latest.*arm-worker"),
    ):
        kubectl.wait_for_ready("test", max_wait=10)
    sleep.assert_not_called()


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
    checked = set()
    kubectl._check_container_platform_logs(pod, "test", checked)
    kubectl._check_container_platform_logs(pod, "test", checked)
    kubectl.core_v1_api.read_namespaced_pod_log.assert_called_once()


def test_log_fetch_failure_does_not_become_a_platform_error():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.read_namespaced_pod_log.side_effect = RuntimeError("log file not yet available")
    kubectl._check_container_platform_logs(_failed_pod(), "test", set())


def test_successful_init_container_does_not_trigger_log_inspection():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl._check_container_platform_logs(_failed_pod(init=True, exit_code=0), "test", set())
    kubectl.core_v1_api.read_namespaced_pod_log.assert_not_called()


def test_restarting_container_uses_previous_logs():
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.read_namespaced_pod_log.return_value = "Connection refused\n"
    pod = _failed_pod()
    status = pod.status.container_statuses[0]
    status.last_state = status.state
    status.state = client.V1ContainerState(running=client.V1ContainerStateRunning())
    kubectl._check_container_platform_logs(pod, "test", set())
    assert kubectl.core_v1_api.read_namespaced_pod_log.call_args.kwargs["previous"] is True
