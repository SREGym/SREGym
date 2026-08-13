from types import SimpleNamespace

from sregym.conductor.oracles.admission_webhook_outage_mitigation import (
    AdmissionWebhookOutageMitigationOracle,
)


def _deployment(
    *,
    replicas=1,
    generation=2,
    observed_generation=2,
    updated=1,
    ready=1,
    available=1,
    unavailable=0,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="recommendation", generation=generation),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=unavailable,
        ),
    )


def _replica_set(name="recommendation-rs", replicas=1):
    return SimpleNamespace(metadata=SimpleNamespace(name=name), spec=SimpleNamespace(replicas=replicas))


def _pod(name, uid, replica_set="recommendation-rs", *, ready=True, deleting=False):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            uid=uid,
            deletion_timestamp="now" if deleting else None,
            owner_references=[SimpleNamespace(kind="ReplicaSet", name=replica_set)],
        ),
        status=SimpleNamespace(
            phase="Running" if ready else "Pending",
            container_statuses=[SimpleNamespace(ready=ready)],
        ),
    )


def _endpoint(pod_name):
    return SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=pod_name))


class _CoreV1:
    def __init__(self, *, replacement=True, replacement_endpoint=True, initial_endpoint=True):
        self.replacement = replacement
        self.replacement_endpoint = replacement_endpoint
        self.initial_endpoint = initial_endpoint
        self.deleted = False
        self.deletions = []

    def read_namespaced_endpoints(self, name, namespace):
        if not self.deleted:
            names = ["recommendation-old"] if self.initial_endpoint else []
        elif self.replacement and self.replacement_endpoint:
            names = ["recommendation-new"]
        else:
            names = []
        return SimpleNamespace(subsets=[SimpleNamespace(addresses=[_endpoint(name) for name in names])])

    def delete_namespaced_pod(self, name, namespace, body):
        self.deleted = True
        self.deletions.append((name, namespace, body.grace_period_seconds))


class _KubeCtl:
    def __init__(
        self,
        *,
        deployment=None,
        core_v1=None,
        replica_sets=None,
        get_error=None,
        unrelated_pods=None,
    ):
        self.initial_deployment = deployment or _deployment()
        self.core_v1_api = core_v1 or _CoreV1()
        self.replica_sets = [_replica_set()] if replica_sets is None else replica_sets
        self.get_error = get_error
        self.unrelated_pods = unrelated_pods or []

    def get_deployment(self, name, namespace):
        if self.get_error is not None:
            raise self.get_error
        if self.core_v1_api.deleted and not self.core_v1_api.replacement:
            return _deployment(updated=1, ready=0, available=0, unavailable=1)
        return self.initial_deployment

    def get_matching_replicasets(self, namespace, deployment_name):
        return self.replica_sets

    def list_pods(self, namespace):
        if self.core_v1_api.deleted:
            target_pods = [_pod("recommendation-new", "uid-new")] if self.core_v1_api.replacement else []
        else:
            target_pods = [_pod("recommendation-old", "uid-old")]
        return SimpleNamespace(items=[*target_pods, *self.unrelated_pods])


def _oracle(kubectl):
    problem = SimpleNamespace(
        namespace="hotel-reservation",
        faulty_service="recommendation",
        kubectl=kubectl,
    )
    oracle = AdmissionWebhookOutageMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle.replacement_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_accepts_only_after_a_different_serving_pod_is_created():
    core_v1 = _CoreV1()
    unrelated = _pod("maintenance-error", "other-uid", "other-rs", ready=False)

    assert _oracle(_KubeCtl(core_v1=core_v1, unrelated_pods=[unrelated])).evaluate()["success"] is True
    assert core_v1.deletions == [("recommendation-old", "hotel-reservation", 0)]


def test_rejects_broken_admission_after_deleting_current_pod():
    core_v1 = _CoreV1(replacement=False)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deletions) == 1


def test_rejects_scaled_to_zero_without_deleting_a_pod():
    core_v1 = _CoreV1()
    deployment = _deployment(replicas=0, updated=0, ready=0, available=0)

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.deletions == []


def test_rejects_initially_unhealthy_deployment_without_deleting_another_pod():
    core_v1 = _CoreV1()
    deployment = _deployment(ready=0, available=0, unavailable=1)

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.deletions == []


def test_rejects_deleted_deployment_without_deleting_a_pod():
    core_v1 = _CoreV1()

    assert _oracle(_KubeCtl(core_v1=core_v1, get_error=RuntimeError("not found"))).evaluate()["success"] is False
    assert core_v1.deletions == []


def test_rejects_initial_pod_that_is_not_a_current_service_endpoint():
    core_v1 = _CoreV1(initial_endpoint=False)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.deletions == []


def test_rejects_replacement_that_never_becomes_a_service_endpoint():
    core_v1 = _CoreV1(replacement_endpoint=False)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deletions) == 1


def test_rejects_target_without_an_active_replicaset():
    core_v1 = _CoreV1()

    assert _oracle(_KubeCtl(core_v1=core_v1, replica_sets=[_replica_set(replicas=0)])).evaluate()["success"] is False
    assert core_v1.deletions == []
