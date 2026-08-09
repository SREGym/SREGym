from types import SimpleNamespace

from sregym.conductor.oracles.duplicate_pvc_mounts_mitigation import DuplicatePVCMountsMitigationOracle


def _deployment(
    *,
    replicas=1,
    total=None,
    generation=2,
    observed_generation=2,
    updated=1,
    ready=1,
    available=1,
    unavailable=0,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="jaeger", generation=generation),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            replicas=replicas if total is None else total,
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


def _pod(
    name,
    replica_set,
    *,
    deleting=False,
    phase="Running",
    ready=True,
    pod_ip="10.244.0.10",
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            deletion_timestamp="now" if deleting else None,
            owner_references=[SimpleNamespace(kind="ReplicaSet", name=replica_set)],
        ),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=[SimpleNamespace(ready=ready)],
            pod_ip=pod_ip,
        ),
    )


class _CoreV1:
    def __init__(
        self,
        *,
        check_phase="Succeeded",
        check_logs="SERVICE_OK\n",
    ):
        self.check_phase = check_phase
        self.check_logs = check_logs
        self.created_pods = []
        self.deleted_pods = []

    def read_namespaced_endpoints(self, name, namespace):
        raise AssertionError("ExternalName services do not provide an Endpoints object")

    def read_namespaced_service(self, name, namespace):
        raise AssertionError("The target pod probe must not depend on the ExternalName service")

    def create_namespaced_pod(self, namespace, body):
        self.created_pods.append((namespace, body))

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(phase=self.check_phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.check_logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted_pods.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(
        self,
        *,
        deployment=None,
        replica_sets=None,
        pods=None,
        core_v1=None,
        get_error=None,
    ):
        self.deployment = deployment or _deployment()
        self.replica_sets = (
            [_replica_set("jaeger-current-rs", 1), _replica_set("jaeger-old-rs", 0)]
            if replica_sets is None
            else replica_sets
        )
        self.pods = [_pod("jaeger-current", "jaeger-current-rs")] if pods is None else pods
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
        faulty_service="jaeger",
        kubectl=kubectl,
    )
    oracle = DuplicatePVCMountsMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_accepts_healthy_single_replica_and_ignores_obsolete_or_unrelated_pods():
    core_v1 = _CoreV1()
    pods = [
        _pod("jaeger-current", "jaeger-current-rs"),
        _pod("jaeger-old", "jaeger-old-rs", deleting=True),
        _pod("unrelated", "other-rs"),
    ]

    assert _oracle(_KubeCtl(pods=pods, core_v1=core_v1)).evaluate()["success"] is True
    assert len(core_v1.created_pods) == 1
    check = core_v1.created_pods[0][1]
    script = check.spec.containers[0].command[-1]
    assert check.metadata.name.startswith("service-content-check-")
    assert not any(word in check.metadata.name for word in ("sregym", "fault", "oracle", "mitigation"))
    assert "http://10.244.0.10:16686/api/services" in script
    assert len(core_v1.deleted_pods) == 1


def test_rejects_injected_partial_rollout_without_starting_check():
    core_v1 = _CoreV1()
    deployment = _deployment(replicas=2, updated=2, ready=1, available=1, unavailable=1)

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_scaled_to_zero_without_starting_check():
    core_v1 = _CoreV1()
    deployment = _deployment(replicas=0, updated=0, ready=0, available=0)

    assert _oracle(_KubeCtl(deployment=deployment, pods=[], core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_deleted_deployment_or_statefulset_only_replacement():
    core_v1 = _CoreV1()

    assert _oracle(_KubeCtl(core_v1=core_v1, get_error=RuntimeError("not found"))).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_stale_generation_with_one_old_ready_replica():
    core_v1 = _CoreV1()
    deployment = _deployment(
        replicas=2,
        generation=3,
        observed_generation=2,
        updated=1,
        ready=1,
        available=1,
        unavailable=1,
    )

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_old_ready_pod_masking_unready_current_rollout():
    core_v1 = _CoreV1()
    deployment = _deployment(
        replicas=1,
        total=2,
        updated=1,
        ready=1,
        available=1,
        unavailable=0,
    )
    replica_sets = [
        _replica_set("jaeger-current-rs", 1),
        _replica_set("jaeger-old-rs", 1),
    ]
    pods = [
        _pod("jaeger-current", "jaeger-current-rs", ready=False),
        _pod("jaeger-old", "jaeger-old-rs"),
    ]

    assert (
        _oracle(
            _KubeCtl(
                deployment=deployment,
                replica_sets=replica_sets,
                pods=pods,
                core_v1=core_v1,
            )
        ).evaluate()["success"]
        is False
    )
    assert core_v1.created_pods == []


def test_rejects_ready_pod_owned_only_by_an_obsolete_replicaset():
    core_v1 = _CoreV1()
    pods = [
        _pod("jaeger-current", "jaeger-current-rs", ready=False),
        _pod("jaeger-old", "jaeger-old-rs"),
    ]

    assert _oracle(_KubeCtl(pods=pods, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_replacement_without_an_active_target_replicaset():
    core_v1 = _CoreV1()

    assert (
        _oracle(_KubeCtl(replica_sets=[_replica_set("jaeger-old-rs", 0)], core_v1=core_v1)).evaluate()["success"]
        is False
    )
    assert core_v1.created_pods == []


def test_rejects_current_pod_without_an_ip_without_starting_check():
    core_v1 = _CoreV1()
    pods = [_pod("jaeger-current", "jaeger-current-rs", pod_ip=None)]

    assert _oracle(_KubeCtl(pods=pods, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_current_pod_that_is_not_ready_without_starting_check():
    core_v1 = _CoreV1()
    pods = [_pod("jaeger-current", "jaeger-current-rs", ready=False)]

    assert _oracle(_KubeCtl(pods=pods, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_failed_query_and_cleans_up_check_pod():
    core_v1 = _CoreV1(check_phase="Failed", check_logs="wget: connection refused\n")

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deleted_pods) == 1
