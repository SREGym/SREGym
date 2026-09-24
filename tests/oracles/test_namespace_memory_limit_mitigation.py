from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.namespace_memory_limit_mitigation import (
    NamespaceMemoryLimitMitigationOracle,
)


def _quota(name, hard):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(hard=hard),
    )


def _deployment(
    *,
    replicas=1,
    generation=1,
    observed_generation=1,
    current_replicas=1,
    updated=1,
    ready=1,
    available=1,
    unavailable=0,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="search", generation=generation),
        spec=SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(match_labels={"io.kompose.service": "search"}),
        ),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            replicas=current_replicas,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=unavailable,
        ),
    )


def _pod(name, app="search"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={"io.kompose.service": app}),
    )


def _endpoint(pod_name):
    return SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=pod_name))


class _CoreV1:
    def __init__(
        self,
        *,
        endpoint_pods=None,
        probe_phase="Succeeded",
        probe_logs="SEARCH_OK\n",
        create_error=None,
    ):
        endpoint_pods = ["search-abc"] if endpoint_pods is None else endpoint_pods
        self.endpoints = SimpleNamespace(
            subsets=[SimpleNamespace(addresses=[_endpoint(name) for name in endpoint_pods])]
        )
        self.probe_phase = probe_phase
        self.probe_logs = probe_logs
        self.create_error = create_error
        self.created_pods = []
        self.deleted_pods = []

    def read_namespaced_endpoints(self, name, namespace):
        return self.endpoints

    def read_namespaced_service(self, name, namespace):
        return SimpleNamespace(spec=SimpleNamespace(ports=[SimpleNamespace(port=8082)]))

    def create_namespaced_pod(self, namespace, body):
        self.created_pods.append((namespace, body))
        if self.create_error is not None:
            raise self.create_error

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(phase=self.probe_phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.probe_logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted_pods.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, *, deployment=None, quotas=None, pods=None, core_v1=None):
        self.deployment = deployment or _deployment()
        self.quotas = list(quotas or [])
        self.pods = [_pod("search-abc")] if pods is None else pods
        self.core_v1_api = core_v1 or _CoreV1()

    def get_resource_quotas(self, namespace):
        return self.quotas

    def get_deployment(self, name, namespace):
        return self.deployment

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)


def _oracle(kubectl):
    problem = SimpleNamespace(
        namespace="hotel-reservation",
        faulty_service="search",
        kubectl=kubectl,
    )
    oracle = NamespaceMemoryLimitMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_accepts_removed_memory_quota_and_fresh_search_connection():
    core_v1 = _CoreV1()
    oracle = _oracle(_KubeCtl(core_v1=core_v1))

    assert oracle.evaluate()["success"] is True
    assert len(core_v1.created_pods) == 1
    probe = core_v1.created_pods[0][1]
    assert probe.spec.containers[0].resources is None
    assert "nc -z -w 3" in probe.spec.containers[0].command[-1]
    assert len(core_v1.deleted_pods) == 1


@pytest.mark.parametrize("hard", [{}, {"pods": "30"}])
def test_accepts_quota_object_after_its_memory_constraint_is_removed(hard):
    quotas = [_quota("memory-limit-quota", hard)]

    assert _oracle(_KubeCtl(quotas=quotas)).evaluate()["success"] is True


@pytest.mark.parametrize("memory_key", ["memory", "requests.memory", "limits.memory"])
def test_rejects_any_remaining_namespace_memory_enforcement(memory_key):
    core_v1 = _CoreV1()
    quotas = [_quota("renamed-or-original-policy", {memory_key: "100Gi"})]

    assert _oracle(_KubeCtl(quotas=quotas, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_search_only_resource_repair_while_quota_remains():
    core_v1 = _CoreV1()
    quotas = [_quota("memory-limit-quota", {"memory": "1Gi"})]

    assert _oracle(_KubeCtl(quotas=quotas, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_scaled_to_zero_without_starting_probe():
    core_v1 = _CoreV1()
    deployment = _deployment(
        replicas=0,
        current_replicas=0,
        updated=0,
        ready=0,
        available=0,
    )

    assert _oracle(_KubeCtl(deployment=deployment, pods=[], core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_an_available_old_replica_during_a_stale_rollout():
    core_v1 = _CoreV1()
    deployment = _deployment(
        generation=2,
        observed_generation=1,
        updated=0,
        ready=1,
        available=1,
        unavailable=1,
    )

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_service_endpoint_owned_by_another_workload():
    core_v1 = _CoreV1(endpoint_pods=["frontend-abc"])
    pods = [_pod("search-abc"), _pod("frontend-abc", app="frontend")]

    assert _oracle(_KubeCtl(pods=pods, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_failed_fresh_connection_and_cleans_up_probe():
    core_v1 = _CoreV1(probe_phase="Failed", probe_logs="")

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deleted_pods) == 1


def test_rejects_fresh_pod_admission_failure_and_attempts_cleanup():
    core_v1 = _CoreV1(create_error=ApiException(status=403, reason="must specify memory"))

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deleted_pods) == 1
