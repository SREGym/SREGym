import json
from types import SimpleNamespace

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
    def __init__(self, succeeded_output):
        self.succeeded_output = succeeded_output
        self.commands = []

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
    metadata = {"name": name, "namespace": namespace}
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

    assert fake.commands == [
        FAILED,
        SUCCEEDED,
        "kubectl delete pod openebs-ndm-operator-5849bb84b8-spzgs -n openebs --ignore-not-found=true",
        "kubectl delete pod db-0 -n data --ignore-not-found=true",
        "kubectl delete pod agent-x7k2p -n kube-system --ignore-not-found=true",
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


def test_unparseable_output_deletes_nothing_more():
    fake = _FakeKubeCtl("error: the server doesn't have a resource type")

    _conductor(fake)._delete_evicted_pods("astronomy-shop")

    assert fake.commands == [FAILED, SUCCEEDED]
