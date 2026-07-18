from types import SimpleNamespace

from sregym.conductor.oracles.readiness_probe_mitigation import ReadinessProbeMitigationOracle


def _deployment(
    *,
    replicas=1,
    generation=2,
    observed_generation=2,
    total_replicas=1,
    updated=1,
    ready=1,
    available=1,
    unavailable=0,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="user-service", generation=generation),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            replicas=total_replicas,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=unavailable,
        ),
    )


def _replica_set(name, replicas):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(replicas=replicas),
    )


def _pod(name, replica_set, *, deleting=False, phase="Running"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            deletion_timestamp="now" if deleting else None,
            owner_references=[SimpleNamespace(kind="ReplicaSet", name=replica_set)],
        ),
        status=SimpleNamespace(phase=phase),
    )


def _endpoint(pod_name):
    return SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=pod_name))


class _CoreV1:
    def __init__(self, *, endpoint_pods=None, check_phase="Succeeded", check_logs="SERVICE_OK\n"):
        endpoint_pods = ["user-current"] if endpoint_pods is None else endpoint_pods
        self.endpoints = SimpleNamespace(
            subsets=[SimpleNamespace(addresses=[_endpoint(name) for name in endpoint_pods])]
        )
        self.check_phase = check_phase
        self.check_logs = check_logs
        self.created_pods = []
        self.deleted_pods = []

    def read_namespaced_endpoints(self, name, namespace):
        return self.endpoints

    def read_namespaced_service(self, name, namespace):
        return SimpleNamespace(spec=SimpleNamespace(ports=[SimpleNamespace(port=9090)]))

    def create_namespaced_pod(self, namespace, body):
        self.created_pods.append((namespace, body))

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(phase=self.check_phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.check_logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted_pods.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, *, deployment=None, replica_sets=None, pods=None, core_v1=None, get_error=None):
        self.deployment = deployment or _deployment()
        self.replica_sets = (
            [_replica_set("user-current-rs", 1), _replica_set("user-old-rs", 0)]
            if replica_sets is None
            else replica_sets
        )
        self.pods = [_pod("user-current", "user-current-rs")] if pods is None else pods
        self.core_v1_api = core_v1 or _CoreV1()
        self.get_error = get_error

    def get_deployment(self, name, namespace):
        if self.get_error is not None:
            raise self.get_error
        return self.deployment

    def get_matching_replicasets(self, namespace, deployment_name):
        return self.replica_sets

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)


def _oracle(kubectl):
    problem = SimpleNamespace(
        namespace="social-network",
        faulty_service="user-service",
        kubectl=kubectl,
    )
    oracle = ReadinessProbeMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_accepts_current_endpoint_while_ignoring_old_failed_and_unrelated_pods():
    core_v1 = _CoreV1()
    deployment = _deployment(total_replicas=2)
    pods = [
        _pod("user-current", "user-current-rs"),
        _pod("user-old-error", "user-old-rs", phase="Failed"),
        _pod("unrelated-error", "other-rs", phase="Failed"),
    ]

    assert _oracle(_KubeCtl(deployment=deployment, pods=pods, core_v1=core_v1)).evaluate()["success"] is True
    assert len(core_v1.created_pods) == 1
    check = core_v1.created_pods[0][1]
    assert check.metadata.name.startswith("service-connectivity-check-")
    assert "user-service.social-network.svc.cluster.local" in check.spec.containers[0].command[-1]
    assert " 9090" in check.spec.containers[0].command[-1]
    assert len(core_v1.deleted_pods) == 1


def test_rejects_injected_not_ready_rollout_without_starting_check():
    core_v1 = _CoreV1()
    deployment = _deployment(total_replicas=1, updated=1, ready=0, available=0, unavailable=1)

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_scaled_to_zero_without_starting_check():
    core_v1 = _CoreV1()
    deployment = _deployment(
        replicas=0,
        total_replicas=0,
        updated=0,
        ready=0,
        available=0,
    )

    assert _oracle(_KubeCtl(deployment=deployment, pods=[], core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_deleted_target_deployment():
    core_v1 = _CoreV1()

    assert _oracle(_KubeCtl(core_v1=core_v1, get_error=RuntimeError("not found"))).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_stale_generation_with_old_ready_replica():
    core_v1 = _CoreV1()
    deployment = _deployment(
        generation=3,
        observed_generation=2,
        total_replicas=1,
        updated=0,
        ready=1,
        available=1,
        unavailable=1,
    )

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_endpoint_from_old_replica_set():
    core_v1 = _CoreV1(endpoint_pods=["user-old"])
    pods = [
        _pod("user-current", "user-current-rs"),
        _pod("user-old", "user-old-rs"),
    ]

    assert _oracle(_KubeCtl(pods=pods, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_replacement_workload_without_active_target_replicaset():
    core_v1 = _CoreV1()
    replica_sets = [_replica_set("user-old-rs", 0)]

    assert _oracle(_KubeCtl(replica_sets=replica_sets, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_failed_fresh_connection_and_cleans_up_check():
    core_v1 = _CoreV1(check_phase="Failed", check_logs="")

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deleted_pods) == 1
