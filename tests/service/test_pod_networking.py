from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.service.kubectl import KubeCtl


def _node(name, *, os_name="linux", cordoned=False):
    return client.V1Node(
        metadata=client.V1ObjectMeta(name=name, labels={"kubernetes.io/os": os_name}),
        spec=client.V1NodeSpec(unschedulable=cordoned),
    )


@pytest.fixture
def networking(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("sregym.service.kubectl.time.monotonic", lambda: now[0])
    monkeypatch.setattr("sregym.service.kubectl.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    subject = KubeCtl.__new__(KubeCtl)
    api = subject.core_v1_api = Mock()
    api.list_node.return_value = client.V1NodeList(items=[_node("worker-a"), _node("worker-b")])
    api.list_namespaced_event.return_value = client.CoreV1EventList(items=[])
    pods = {}

    def create(namespace, body, **kwargs):
        pods[body.metadata.name] = body
        body.metadata.uid = f"uid-{body.metadata.name}"
        body.status = client.V1PodStatus(
            phase="Running", pod_ip="10.0.0.7", conditions=[client.V1PodCondition(type="Ready", status="True")]
        )
        return body

    api.create_namespaced_pod.side_effect = create
    api.read_namespaced_pod.side_effect = lambda name, namespace, **kwargs: pods[name]
    monkeypatch.delenv("SREGYM_NETWORK_PROBE_IMAGE", raising=False)
    return subject, api, pods


def test_networking_probes_every_uncordoned_linux_node_before_waiting(networking):
    subject, api, _ = networking
    api.list_node.return_value.items.extend([_node("cordoned", cordoned=True), _node("windows", os_name="windows")])

    subject.assert_pod_networking(timeout=1)

    creates = api.create_namespaced_pod.call_args_list
    assert {call.kwargs["body"].spec.node_name for call in creates} == {"worker-a", "worker-b"}
    assert api.method_calls[1:3] == [call for call in api.method_calls if call[0] == "create_namespaced_pod"]
    assert api.delete_namespaced_pod.call_count == 2
    for call in creates:
        pod = call.kwargs["body"]
        assert pod.spec.automount_service_account_token is False
        assert pod.spec.tolerations[0].operator == "Exists"
        assert pod.spec.containers[0].image_pull_policy == "IfNotPresent"


def test_one_healthy_node_cannot_hide_a_broken_worker(networking):
    subject, api, pods = networking

    def read(name, namespace, **kwargs):
        pod = pods[name]
        if pod.spec.node_name == "worker-b":
            pod.status = client.V1PodStatus(phase="Pending")
        return pod

    api.read_namespaced_pod.side_effect = read
    api.list_namespaced_event.return_value.items = [
        client.CoreV1Event(
            metadata=client.V1ObjectMeta(),
            involved_object=client.V1ObjectReference(),
            type="Warning",
            reason="FailedCreatePodSandBox",
            message="Calico: Unauthorized",
        )
    ]

    with pytest.raises(RuntimeError, match="worker-b.*FailedCreatePodSandBox.*Unauthorized"):
        subject.assert_pod_networking(timeout=2)

    assert api.delete_namespaced_pod.call_count == 2
    field = api.list_namespaced_event.call_args.kwargs["field_selector"]
    assert field.startswith("involvedObject.uid=")
    methods = [call[0] for call in api.method_calls]
    assert methods.index("list_namespaced_event") < methods.index("delete_namespaced_pod")


def test_ready_pod_without_an_ip_does_not_pass(networking):
    subject, api, pods = networking

    def read(name, namespace, **kwargs):
        pod = pods[name]
        pod.status.pod_ip = None
        return pod

    api.read_namespaced_pod.side_effect = read
    with pytest.raises(RuntimeError, match="no pod IP"):
        subject.assert_pod_networking(timeout=1)


def test_image_pull_errors_are_not_misreported_as_a_cni_failure(networking):
    subject, api, pods = networking

    def read(name, namespace, **kwargs):
        pod = pods[name]
        pod.status = client.V1PodStatus(
            phase="Pending",
            container_statuses=[
                client.V1ContainerStatus(
                    name="pause",
                    image="registry.k8s.io/pause:3.10",
                    image_id="",
                    ready=False,
                    restart_count=0,
                    state=client.V1ContainerState(
                        waiting=client.V1ContainerStateWaiting(
                            reason="ImagePullBackOff", message="registry unavailable"
                        )
                    ),
                )
            ],
        )
        return pod

    api.read_namespaced_pod.side_effect = read
    with pytest.raises(RuntimeError, match="ImagePullBackOff.*registry unavailable") as error:
        subject.assert_pod_networking(timeout=1)
    assert "CNI is unhealthy" not in str(error.value)


def test_custom_networking_probe_image(networking, monkeypatch):
    subject, api, _ = networking
    monkeypatch.setenv("SREGYM_NETWORK_PROBE_IMAGE", "registry.example/pause:local")
    subject.assert_pod_networking(timeout=1)
    assert all(
        call.kwargs["body"].spec.containers[0].image == "registry.example/pause:local"
        for call in api.create_namespaced_pod.call_args_list
    )


def test_no_eligible_nodes_stops_without_creating_probes(networking):
    subject, api, _ = networking
    api.list_node.return_value.items = [_node("cordoned", cordoned=True)]
    with pytest.raises(RuntimeError, match="no uncordoned Linux nodes"):
        subject.assert_pod_networking(timeout=1)
    api.create_namespaced_pod.assert_not_called()


def test_failed_create_is_included_in_cleanup_without_masking_the_error(networking):
    subject, api, _ = networking
    api.create_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")
    api.delete_namespaced_pod.side_effect = TimeoutError("cleanup API unavailable")
    with pytest.raises(RuntimeError, match="API error: 403 Forbidden"):
        subject.assert_pod_networking(timeout=1)
    api.delete_namespaced_pod.assert_called_once()


def test_timed_out_create_is_cleaned_up_even_without_an_api_acknowledgement(networking):
    subject, api, _ = networking
    api.create_namespaced_pod.side_effect = TimeoutError("API did not acknowledge creation")
    with pytest.raises(TimeoutError, match="API did not acknowledge creation"):
        subject.assert_pod_networking(timeout=1)
    api.delete_namespaced_pod.assert_called_once()


def test_read_errors_clean_up_all_created_probes(networking):
    subject, api, _ = networking
    api.read_namespaced_pod.side_effect = ApiException(status=503, reason="Unavailable")
    with pytest.raises(RuntimeError, match="503 Unavailable"):
        subject.assert_pod_networking(timeout=1)
    assert api.delete_namespaced_pod.call_count == 2


def test_failed_cleanup_is_not_silently_accepted(networking):
    subject, api, _ = networking
    api.delete_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")
    with pytest.raises(RuntimeError, match="Could not remove pod networking probes"):
        subject.assert_pod_networking(timeout=1)
    assert api.delete_namespaced_pod.call_count == 2


def test_warning_event_errors_do_not_mask_the_readiness_failure(networking):
    subject, api, pods = networking
    api.read_namespaced_pod.side_effect = lambda name, namespace, **kwargs: client.V1Pod(
        metadata=pods[name].metadata, spec=pods[name].spec, status=client.V1PodStatus(phase="Pending")
    )
    api.list_namespaced_event.side_effect = ApiException(status=403, reason="Forbidden")
    with pytest.raises(RuntimeError, match="warning events unavailable"):
        subject.assert_pod_networking(timeout=1)
    assert api.delete_namespaced_pod.call_count == 2


def test_networking_deadline_reaches_the_real_http_transport():
    subject = KubeCtl.__new__(KubeCtl)
    api_client = client.ApiClient(client.Configuration())
    request = Mock(return_value=Mock(status=200, data=b'{"apiVersion":"v1","kind":"NodeList","items":[]}'))
    api_client.rest_client.pool_manager.request = request
    subject.core_v1_api = client.CoreV1Api(api_client)
    with pytest.raises(RuntimeError, match="no uncordoned Linux nodes"):
        subject.assert_pod_networking(timeout=0.25)
    timeout = request.call_args.kwargs["timeout"]
    assert 0 < timeout.connect_timeout <= 0.25
    assert 0 < timeout.read_timeout <= 0.25
