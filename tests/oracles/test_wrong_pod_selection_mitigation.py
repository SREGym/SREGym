from types import SimpleNamespace

from sregym.conductor.oracles.wrong_pod_selection_mitigation import (
    WrongPodSelectionMitigationOracle,
)


def _deployment(name, replicas=1, ready=1, generation=1, observed_generation=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, generation=generation),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            updated_replicas=ready,
            ready_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=0 if ready == replicas else replicas - ready,
        ),
    )


def _replica_set(name, replicas=1):
    return SimpleNamespace(metadata=SimpleNamespace(name=name), spec=SimpleNamespace(replicas=replicas))


def _pod(name, service, owner):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels={"io.kompose.service": service},
            owner_references=[SimpleNamespace(kind="ReplicaSet", name=owner)],
            deletion_timestamp=None,
        )
    )


def _endpoint(pod_name, ready=True):
    return SimpleNamespace(
        target_ref=SimpleNamespace(kind="Pod", name=pod_name),
        conditions=SimpleNamespace(ready=ready),
    )


class _DiscoveryV1:
    def __init__(self, endpoints):
        self.endpoints = endpoints

    def list_namespaced_endpoint_slice(self, namespace, label_selector):
        return SimpleNamespace(items=[SimpleNamespace(endpoints=self.endpoints)])


class _CoreV1:
    def __init__(self, pods, probe_phase="Succeeded", probe_logs="SERVICE_OK\n"):
        self.pods = {pod.metadata.name: pod for pod in pods}
        self.probe_phase = probe_phase
        self.probe_logs = probe_logs
        self.created = []
        self.deleted = []

    def read_namespaced_pod(self, name, namespace):
        if name in self.pods:
            return self.pods[name]
        return SimpleNamespace(status=SimpleNamespace(phase=self.probe_phase))

    def create_namespaced_pod(self, namespace, body):
        self.created.append((namespace, body))

    def read_namespaced_pod_log(self, name, namespace):
        return self.probe_logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, deployments, core_v1, replica_sets):
        self.deployments = deployments
        self.core_v1_api = core_v1
        self.replica_sets = replica_sets

    def get_deployment(self, name, namespace):
        return self.deployments[name]

    def get_matching_replicasets(self, namespace, deployment_name):
        return self.replica_sets[deployment_name]


def _oracle(
    endpoints,
    pods,
    deployments=None,
    replica_sets=None,
    probe_phase="Succeeded",
    probe_logs="SERVICE_OK\n",
):
    deployments = deployments or {
        "frontend": _deployment("frontend"),
        "search": _deployment("search"),
    }
    replica_sets = replica_sets or {
        "frontend": [_replica_set("frontend-rs")],
        "search": [_replica_set("search-rs")],
    }
    core_v1 = _CoreV1(pods, probe_phase=probe_phase, probe_logs=probe_logs)
    kubectl = _KubeCtl(deployments, core_v1, replica_sets)
    problem = SimpleNamespace(
        namespace="hotel-reservation",
        frontend_service="frontend",
        wrong_deployment="search",
        expected_endpoint_pod_label="frontend",
        expected_service_port=5000,
        kubectl=kubectl,
    )
    oracle = WrongPodSelectionMitigationOracle(problem)
    oracle.discovery_v1 = _DiscoveryV1(endpoints)
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle, core_v1


def test_accepts_frontend_only_ready_endpoints_and_connectivity():
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-abc")],
        [_pod("frontend-abc", "frontend", "frontend-rs")],
    )

    assert oracle.evaluate()["success"] is True
    command = core_v1.created[0][1].spec.containers[0].command[-1]
    assert "frontend.hotel-reservation.svc.cluster.local 5000" in command
    assert len(core_v1.deleted) == 1


def test_rejects_ready_search_endpoint_selected_by_frontend_service():
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-abc"), _endpoint("search-xyz")],
        [
            _pod("frontend-abc", "frontend", "frontend-rs"),
            _pod("search-xyz", "search", "search-rs"),
        ],
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created == []


def test_rejects_frontend_service_that_cannot_accept_tcp_traffic():
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-abc")],
        [_pod("frontend-abc", "frontend", "frontend-rs")],
        probe_phase="Failed",
        probe_logs="",
    )

    assert oracle.evaluate()["success"] is False
    assert len(core_v1.created) == 1


def test_rejects_search_scaled_to_zero_to_hide_wrong_endpoint():
    deployments = {
        "frontend": _deployment("frontend"),
        "search": _deployment("search", replicas=0, ready=0),
    }
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-abc")],
        [_pod("frontend-abc", "frontend", "frontend-rs")],
        deployments=deployments,
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created == []


def test_rejects_stale_frontend_rollout():
    deployments = {
        "frontend": _deployment("frontend", generation=2, observed_generation=1),
        "search": _deployment("search"),
    }
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-abc")],
        [_pod("frontend-abc", "frontend", "frontend-rs")],
        deployments=deployments,
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created == []


def test_ignores_unready_endpoint_slice_entries():
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-abc", ready=False)],
        [_pod("frontend-abc", "frontend", "frontend-rs")],
    )
    core_v1.read_namespaced_endpoints = lambda name, namespace: SimpleNamespace(subsets=[])

    assert oracle.evaluate()["success"] is False
    assert core_v1.created == []


def test_rejects_foreign_pod_relabelled_as_frontend():
    oracle, core_v1 = _oracle(
        [_endpoint("search-xyz")],
        [_pod("search-xyz", "frontend", "search-rs")],
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created == []


def test_rejects_endpoint_from_inactive_frontend_replicaset():
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-old")],
        [_pod("frontend-old", "frontend", "frontend-old-rs")],
        replica_sets={
            "frontend": [_replica_set("frontend-rs"), _replica_set("frontend-old-rs", replicas=0)],
            "search": [_replica_set("search-rs")],
        },
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created == []
