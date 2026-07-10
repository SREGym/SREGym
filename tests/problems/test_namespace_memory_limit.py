from types import SimpleNamespace

import pytest
from kubernetes import client

from sregym.conductor.problems.namespace_memory_limit import NamespaceMemoryLimit


def _quota(name, hard):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(hard=hard),
    )


def _replica_set(name, *, desired=1, current=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(replicas=desired),
        status=SimpleNamespace(replicas=current),
    )


def _deployment(*, replicas=1, memory_request=None):
    requests = {"cpu": "100m"}
    if memory_request is not None:
        requests["memory"] = memory_request
    return SimpleNamespace(
        metadata=SimpleNamespace(name="search", generation=1),
        spec=SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(match_labels={"io.kompose.service": "search"}),
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[
                        client.V1Container(
                            name="hotel-reserv-search",
                            resources=client.V1ResourceRequirements(requests=requests),
                        )
                    ]
                )
            ),
        ),
        status=SimpleNamespace(
            observed_generation=1,
            replicas=replicas,
            updated_replicas=replicas,
            ready_replicas=replicas,
            available_replicas=replicas,
            unavailable_replicas=0,
        ),
    )


def _pod(name="search-abc"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={"io.kompose.service": "search"}),
    )


def _endpoints(name="search-abc"):
    address = SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=name))
    return SimpleNamespace(subsets=[SimpleNamespace(addresses=[address])])


class _CoreV1:
    def read_namespaced_endpoints(self, name, namespace):
        return _endpoints()


class _KubeCtl:
    def __init__(self, *, deployment=None, quotas=None, replica_sets=None):
        self.deployment = deployment or _deployment()
        self.quotas = list(quotas or [])
        self.replica_sets = list(replica_sets or [_replica_set("search-current")])
        self.core_v1_api = _CoreV1()
        self.applied = []
        self.deleted_quotas = []
        self.deleted_replica_sets = []
        self.scales = []

    def get_deployment(self, name, namespace):
        return self.deployment

    def get_resource_quotas(self, namespace):
        return self.quotas

    def get_matching_replicasets(self, namespace, deployment_name):
        return self.replica_sets

    def apply_resource(self, body):
        self.applied.append(body)

    def delete_replicaset(self, name, namespace):
        self.deleted_replica_sets.append((name, namespace))
        self.deployment.status.ready_replicas = 0
        self.deployment.status.available_replicas = 0
        self.deployment.status.unavailable_replicas = self.deployment.spec.replicas

    def delete_resource_quota(self, name, namespace):
        self.deleted_quotas.append((name, namespace))
        self.quotas = [quota for quota in self.quotas if quota.metadata.name != name]

    def scale_deployment(self, name, namespace, replicas):
        self.scales.append((name, namespace, replicas))
        self.deployment.metadata.generation += 1
        self.deployment.spec.replicas = replicas
        self.deployment.status.observed_generation = self.deployment.metadata.generation
        self.deployment.status.replicas = replicas
        self.deployment.status.updated_replicas = replicas
        self.deployment.status.ready_replicas = replicas
        self.deployment.status.available_replicas = replicas
        self.deployment.status.unavailable_replicas = 0

    def list_pods(self, namespace):
        return SimpleNamespace(items=[_pod()])


def _problem(kubectl):
    problem = NamespaceMemoryLimit.__new__(NamespaceMemoryLimit)
    problem.kubectl = kubectl
    problem.namespace = "hotel-reservation"
    problem.faulty_service = "search"
    problem._baseline_replicas = None
    problem.rollout_timeout_seconds = 0
    problem.fault_timeout_seconds = 0
    problem.poll_interval_seconds = 0
    return problem


def test_injection_creates_owned_quota_and_deletes_only_active_replica_sets():
    kubectl = _KubeCtl(
        replica_sets=[
            _replica_set("search-current", desired=1, current=1),
            _replica_set("search-old", desired=0, current=0),
        ]
    )
    problem = _problem(kubectl)

    problem.inject_fault()

    assert problem._baseline_replicas == 1
    assert kubectl.applied == [
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {
                "name": "memory-limit-quota",
                "namespace": "hotel-reservation",
            },
            "spec": {"hard": {"memory": "1Gi"}},
        }
    ]
    assert kubectl.deleted_replica_sets == [("search-current", "hotel-reservation")]
    assert problem.fault_injected is True


@pytest.mark.parametrize(
    ("quotas", "message"),
    [
        ([_quota("memory-limit-quota", {})], "already exists"),
        ([_quota("existing-policy", {"requests.memory": "4Gi"})], "existing memory ResourceQuota"),
    ],
)
def test_injection_rejects_an_unsafe_or_ambiguous_quota_baseline(quotas, message):
    kubectl = _KubeCtl(quotas=quotas)
    problem = _problem(kubectl)

    with pytest.raises(RuntimeError, match=message):
        problem.inject_fault()

    assert kubectl.applied == []
    assert kubectl.deleted_replica_sets == []


def test_injection_rejects_a_target_that_already_declares_memory():
    kubectl = _KubeCtl(deployment=_deployment(memory_request="64Mi"))

    with pytest.raises(RuntimeError, match="already declares memory requests"):
        _problem(kubectl).inject_fault()

    assert kubectl.applied == []


def test_recovery_deletes_only_owned_quota_and_restores_baseline_replica_count():
    injected = _quota("memory-limit-quota", {"memory": "1Gi"})
    unrelated = _quota("unrelated-policy", {"pods": "30"})
    kubectl = _KubeCtl(deployment=_deployment(replicas=2), quotas=[injected, unrelated])
    problem = _problem(kubectl)
    problem._baseline_replicas = 3

    problem.recover_fault()

    assert kubectl.deleted_quotas == [("memory-limit-quota", "hotel-reservation")]
    assert kubectl.quotas == [unrelated]
    assert kubectl.scales == [
        ("search", "hotel-reservation", 0),
        ("search", "hotel-reservation", 3),
    ]
    assert problem._baseline_replicas is None
    assert problem.fault_injected is False


def test_recovery_is_idempotent_when_the_agent_already_removed_the_quota():
    kubectl = _KubeCtl(quotas=[_quota("unrelated-policy", {"pods": "30"})])
    problem = _problem(kubectl)

    problem.recover_fault()

    assert kubectl.deleted_quotas == []
    assert kubectl.scales[-1] == ("search", "hotel-reservation", 1)
    assert problem.fault_injected is False
