import json
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.conductor import Conductor

FAILED = (
    "kubectl delete pods --all-namespaces "
    "--field-selector=status.phase=Failed,metadata.namespace!=astronomy-shop --ignore-not-found=true"
)
SUCCEEDED = (
    "kubectl get pods --all-namespaces "
    "--field-selector=status.phase=Succeeded,metadata.namespace!=astronomy-shop -o json"
)


class _FakeKubeCtl:
    def __init__(self, succeeded_output, *, delete_error=None):
        self.succeeded_output = succeeded_output
        self.commands = []
        self.deletions = []
        self.delete_error = delete_error
        self.core_v1_api = SimpleNamespace(delete_namespaced_pod=self.delete_pod)

    def delete_pod(self, **kwargs):
        self.deletions.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error

    def exec_command(self, command, input_data=None):
        self.commands.append(command)
        if command == SUCCEEDED:
            return self.succeeded_output
        return ""


def _conductor(fake_kubectl):
    conductor = object.__new__(Conductor)
    conductor.kubectl = fake_kubectl
    conductor.logger = SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)
    return conductor


def _pod(name, namespace, owner_kind=None):
    metadata = {"name": name, "namespace": namespace, "uid": f"{name}-original-uid"}
    if owner_kind:
        metadata["ownerReferences"] = [{"kind": owner_kind, "name": f"{name}-owner"}]
    return {"metadata": metadata}


def test_deletes_succeeded_pods_owned_by_a_workload_controller():
    fake = _FakeKubeCtl(
        json.dumps(
            {
                "items": [
                    _pod("openebs-ndm-operator-5849bb84b8-spzgs", "openebs", "ReplicaSet"),
                    _pod("db-0", "data", "StatefulSet"),
                    _pod("agent-x7k2p", "kube-system", "DaemonSet"),
                ]
            }
        )
    )

    _conductor(fake)._delete_evicted_pods("astronomy-shop")

    assert fake.commands == [FAILED, SUCCEEDED]
    assert fake.deletions == [
        {
            "name": name,
            "namespace": namespace,
            "body": {"preconditions": {"uid": f"{name}-original-uid"}},
            "_request_timeout": 30,
        }
        for name, namespace in [
            ("openebs-ndm-operator-5849bb84b8-spzgs", "openebs"),
            ("db-0", "data"),
            ("agent-x7k2p", "kube-system"),
        ]
    ]


def test_leaves_completed_job_pods_and_bare_pods_alone():
    fake = _FakeKubeCtl(
        json.dumps(
            {
                "items": [
                    _pod("helm-install-traefik-abcde", "kube-system", "Job"),
                    _pod("one-off", "default"),
                ]
            }
        )
    )

    _conductor(fake)._delete_evicted_pods("astronomy-shop")

    assert fake.commands == [FAILED, SUCCEEDED]
    assert fake.deletions == []


@pytest.mark.parametrize("output", ["error: the server doesn't have a resource type", "null", "[]"])
def test_unparseable_output_deletes_nothing_more(output):
    fake = _FakeKubeCtl(output)

    _conductor(fake)._delete_evicted_pods("astronomy-shop")

    assert fake.commands == [FAILED, SUCCEEDED]
    assert fake.deletions == []


def test_does_not_delete_a_statefulset_replacement_with_the_same_name():
    old_pod = _pod("db-0", "data", "StatefulSet")
    fake = _FakeKubeCtl(json.dumps({"items": [old_pod]}))
    current_pod = _pod("db-0", "data", "StatefulSet")
    current_pod["metadata"]["uid"] = "healthy-replacement-uid"

    def delete_current_pod(*, name, namespace, body, _request_timeout):
        fake.deletions.append(body)
        assert (name, namespace) == ("db-0", "data")
        assert _request_timeout == 30
        assert body["preconditions"]["uid"] == old_pod["metadata"]["uid"]
        if body["preconditions"]["uid"] != current_pod["metadata"]["uid"]:
            raise ApiException(status=409, reason="UID precondition failed")
        current_pod.clear()

    fake.core_v1_api.delete_namespaced_pod = delete_current_pod

    _conductor(fake)._delete_evicted_pods("astronomy-shop")

    assert fake.deletions == [{"preconditions": {"uid": old_pod["metadata"]["uid"]}}]
    assert current_pod["metadata"]["uid"] == "healthy-replacement-uid"


@pytest.mark.parametrize("status", [404, 409])
def test_already_removed_or_replaced_pod_does_not_stop_remaining_cleanup(status):
    fake = _FakeKubeCtl(
        json.dumps({"items": [_pod("old", "openebs", "ReplicaSet"), _pod("next", "openebs", "ReplicaSet")]}),
        delete_error=ApiException(status=status),
    )

    _conductor(fake)._delete_evicted_pods("astronomy-shop")

    assert [request["name"] for request in fake.deletions] == ["old", "next"]


@pytest.mark.parametrize("status", [403, 500, 503])
def test_other_api_errors_are_not_treated_as_successful_cleanup(status):
    error = ApiException(status=status)
    fake = _FakeKubeCtl(json.dumps({"items": [_pod("old", "openebs", "ReplicaSet")]}), delete_error=error)

    with pytest.raises(ApiException) as caught:
        _conductor(fake)._delete_evicted_pods("astronomy-shop")

    assert caught.value is error


def test_transport_error_is_not_treated_as_successful_cleanup():
    error = TimeoutError("API request timed out")
    fake = _FakeKubeCtl(json.dumps({"items": [_pod("old", "openebs", "ReplicaSet")]}), delete_error=error)

    with pytest.raises(TimeoutError) as caught:
        _conductor(fake)._delete_evicted_pods("astronomy-shop")

    assert caught.value is error
