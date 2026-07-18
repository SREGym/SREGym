from types import SimpleNamespace

from sregym.conductor.oracles.mutating_webhook_resource_limits_mitigation import (
    MutatingWebhookResourceLimitsMitigationOracle,
)


def _resources(request="128Mi", limit="256Mi"):
    return SimpleNamespace(
        requests={} if request is None else {"memory": request},
        limits={} if limit is None else {"memory": limit},
    )


def _container(name="nginx-thrift", request="128Mi", limit="256Mi"):
    return SimpleNamespace(name=name, resources=_resources(request, limit))


def _container_status(
    *,
    name="nginx-thrift",
    ready=True,
    restart_count=0,
    terminated_reason=None,
    last_terminated_reason=None,
):
    return SimpleNamespace(
        name=name,
        ready=ready,
        restart_count=restart_count,
        state=SimpleNamespace(
            terminated=(None if terminated_reason is None else SimpleNamespace(reason=terminated_reason))
        ),
        last_state=SimpleNamespace(
            terminated=(None if last_terminated_reason is None else SimpleNamespace(reason=last_terminated_reason))
        ),
    )


def _pod(
    name,
    uid,
    *,
    request="128Mi",
    limit="256Mi",
    ready=True,
    restart_count=0,
    terminated_reason=None,
    last_terminated_reason=None,
    replica_set="nginx-rs",
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            uid=uid,
            deletion_timestamp=None,
            owner_references=[SimpleNamespace(kind="ReplicaSet", name=replica_set)],
        ),
        spec=SimpleNamespace(containers=[_container(request=request, limit=limit)]),
        status=SimpleNamespace(
            phase="Running" if ready else "Pending",
            container_statuses=[
                _container_status(
                    ready=ready,
                    restart_count=restart_count,
                    terminated_reason=terminated_reason,
                    last_terminated_reason=last_terminated_reason,
                )
            ],
        ),
    )


def _deployment(
    *,
    request="128Mi",
    limit="256Mi",
    replicas=1,
    generation=2,
    observed_generation=2,
    updated=1,
    ready=1,
    available=1,
    unavailable=0,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="nginx-thrift", generation=generation),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(spec=SimpleNamespace(containers=[_container(request=request, limit=limit)])),
        ),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=unavailable,
        ),
    )


def _replica_set(replicas=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="nginx-rs"),
        spec=SimpleNamespace(replicas=replicas),
    )


def _endpoint(name):
    return SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=name))


class _CoreV1:
    def __init__(
        self,
        *,
        replacement_request="128Mi",
        replacement_limit="256Mi",
        replacement_ready=True,
        initial_endpoint=True,
        replacement_endpoint=True,
        stability_pod=None,
    ):
        self.replacement_request = replacement_request
        self.replacement_limit = replacement_limit
        self.replacement_ready = replacement_ready
        self.initial_endpoint = initial_endpoint
        self.replacement_endpoint = replacement_endpoint
        self.stability_pod = stability_pod
        self.deleted = False
        self.deletions = []

    def replacement_pod(self):
        return _pod(
            "nginx-new",
            "uid-new",
            request=self.replacement_request,
            limit=self.replacement_limit,
            ready=self.replacement_ready,
        )

    def read_namespaced_endpoints(self, name, namespace):
        if not self.deleted:
            names = ["nginx-old"] if self.initial_endpoint else []
        else:
            names = ["nginx-new"] if self.replacement_endpoint else []
        return SimpleNamespace(subsets=[SimpleNamespace(addresses=[_endpoint(name) for name in names])])

    def delete_namespaced_pod(self, name, namespace, body):
        self.deleted = True
        self.deletions.append((name, namespace, body.grace_period_seconds))

    def read_namespaced_pod(self, name, namespace):
        return self.stability_pod or self.replacement_pod()


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
        self.deployment = deployment or _deployment()
        self.core_v1_api = core_v1 or _CoreV1()
        self.replica_sets = [_replica_set()] if replica_sets is None else replica_sets
        self.get_error = get_error
        self.unrelated_pods = unrelated_pods or []

    def get_deployment(self, name, namespace):
        if self.get_error is not None:
            raise self.get_error
        if self.core_v1_api.deleted and not self.core_v1_api.replacement_ready:
            return _deployment(ready=0, available=0, unavailable=1)
        return self.deployment

    def get_matching_replicasets(self, namespace, deployment_name):
        return self.replica_sets

    def list_pods(self, namespace):
        target = self.core_v1_api.replacement_pod() if self.core_v1_api.deleted else _pod("nginx-old", "uid-old")
        return SimpleNamespace(items=[target, *self.unrelated_pods])


def _oracle(kubectl):
    problem = SimpleNamespace(
        namespace="social-network",
        faulty_service="nginx-thrift",
        kubectl=kubectl,
    )
    oracle = MutatingWebhookResourceLimitsMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle.replacement_timeout_seconds = 0
    oracle.stability_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_accepts_stable_replacement_with_deployment_resources_and_endpoint():
    core_v1 = _CoreV1()
    unrelated = _pod("maintenance", "other-uid", ready=False, replica_set="other-rs")

    assert _oracle(_KubeCtl(core_v1=core_v1, unrelated_pods=[unrelated])).evaluate()["success"] is True
    assert core_v1.deletions == [("nginx-old", "social-network", 0)]


def test_accepts_equivalent_kubernetes_memory_quantities():
    deployment = _deployment(request="0.125Gi", limit="0.25Gi")
    core_v1 = _CoreV1(replacement_request="128Mi", replacement_limit="256Mi")

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is True


def test_accepts_removal_of_explicit_memory_when_replacement_matches():
    deployment = _deployment(request=None, limit=None)
    core_v1 = _CoreV1(replacement_request=None, replacement_limit=None)

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is True


def test_rejects_replacement_mutated_to_harmful_memory_values():
    core_v1 = _CoreV1(
        replacement_request="16Mi",
        replacement_limit="16Mi",
        replacement_ready=False,
    )

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deletions) == 1


def test_rejects_healthy_replacement_that_differs_from_template():
    core_v1 = _CoreV1(replacement_request="512Mi", replacement_limit="512Mi")

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False


def test_rejects_replacement_that_oomkills_during_stability_window():
    unstable = _pod(
        "nginx-new",
        "uid-new",
        last_terminated_reason="OOMKilled",
    )
    core_v1 = _CoreV1(stability_pod=unstable)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False


def test_rejects_replacement_that_restarts_during_stability_window():
    unstable = _pod("nginx-new", "uid-new", restart_count=1)
    core_v1 = _CoreV1(stability_pod=unstable)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False


def test_rejects_replacement_without_service_endpoint():
    core_v1 = _CoreV1(replacement_endpoint=False)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False


def test_rejects_scale_to_zero_without_deleting_pod():
    core_v1 = _CoreV1()
    deployment = _deployment(replicas=0, updated=0, ready=0, available=0)

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.deletions == []


def test_rejects_initially_unhealthy_deployment_without_deleting_pod():
    core_v1 = _CoreV1()
    deployment = _deployment(ready=0, available=0, unavailable=1)

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.deletions == []


def test_rejects_initial_target_without_ready_endpoint():
    core_v1 = _CoreV1(initial_endpoint=False)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.deletions == []


def test_rejects_deleted_deployment_without_deleting_pod():
    core_v1 = _CoreV1()

    assert _oracle(_KubeCtl(core_v1=core_v1, get_error=RuntimeError("not found"))).evaluate()["success"] is False
    assert core_v1.deletions == []
