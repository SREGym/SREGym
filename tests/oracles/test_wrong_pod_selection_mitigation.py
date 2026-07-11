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
    def __init__(self, pod_labels, probe_phase="Succeeded", probe_logs="SERVICE_OK\n"):
        self.pod_labels = pod_labels
        self.probe_phase = probe_phase
        self.probe_logs = probe_logs
        self.created = []
        self.deleted = []

    def read_namespaced_pod(self, name, namespace):
        if name in self.pod_labels:
            return SimpleNamespace(metadata=SimpleNamespace(labels=self.pod_labels[name]))
        return SimpleNamespace(status=SimpleNamespace(phase=self.probe_phase))

    def create_namespaced_pod(self, namespace, body):
        self.created.append((namespace, body))

    def read_namespaced_pod_log(self, name, namespace):
        return self.probe_logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, deployments, core_v1):
        self.deployments = deployments
        self.core_v1_api = core_v1

    def get_deployment(self, name, namespace):
        return self.deployments[name]


def _oracle(
    endpoints,
    pod_labels,
    deployments=None,
    probe_phase="Succeeded",
    probe_logs="SERVICE_OK\n",
):
    deployments = deployments or {
        "frontend": _deployment("frontend"),
        "search": _deployment("search"),
    }
    core_v1 = _CoreV1(pod_labels, probe_phase=probe_phase, probe_logs=probe_logs)
    kubectl = _KubeCtl(deployments, core_v1)
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
        {"frontend-abc": {"io.kompose.service": "frontend"}},
    )

    assert oracle.evaluate()["success"] is True
    command = core_v1.created[0][1].spec.containers[0].command[-1]
    assert "frontend.hotel-reservation.svc.cluster.local 5000" in command
    assert len(core_v1.deleted) == 1


def test_rejects_ready_search_endpoint_selected_by_frontend_service():
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-abc"), _endpoint("search-xyz")],
        {
            "frontend-abc": {"io.kompose.service": "frontend"},
            "search-xyz": {"io.kompose.service": "search"},
        },
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created == []


def test_rejects_frontend_service_that_cannot_accept_tcp_traffic():
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-abc")],
        {"frontend-abc": {"io.kompose.service": "frontend"}},
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
        {"frontend-abc": {"io.kompose.service": "frontend"}},
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
        {"frontend-abc": {"io.kompose.service": "frontend"}},
        deployments=deployments,
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created == []


def test_ignores_unready_endpoint_slice_entries():
    oracle, core_v1 = _oracle(
        [_endpoint("frontend-abc", ready=False)],
        {"frontend-abc": {"io.kompose.service": "frontend"}},
    )
    core_v1.read_namespaced_endpoints = lambda name, namespace: SimpleNamespace(subsets=[])

    assert oracle.evaluate()["success"] is False
    assert core_v1.created == []
