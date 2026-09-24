from types import SimpleNamespace

from sregym.conductor.oracles.internal_traffic_policy_mitigation import (
    InternalTrafficPolicyMitigationOracle,
)


def _endpoint(node, ready):
    return SimpleNamespace(
        node_name=node,
        conditions=SimpleNamespace(ready=ready),
    )


class _CoreV1:
    def __init__(self, policy="Local"):
        self.policy = policy

    def read_namespaced_service(self, name, namespace):
        return SimpleNamespace(spec=SimpleNamespace(internal_traffic_policy=self.policy))


class _DiscoveryV1:
    def __init__(self, endpoints):
        self.endpoints = endpoints
        self.requests = []

    def list_namespaced_endpoint_slice(self, namespace, label_selector):
        self.requests.append((namespace, label_selector))
        return SimpleNamespace(items=[SimpleNamespace(endpoints=self.endpoints)])


def _oracle(endpoints, policy="Local"):
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        FAULTY_SERVICE="recommendation",
        SERVICE_PORT=8080,
        victim_node="worker-b",
        worker_nodes=lambda: ["worker-a", "worker-b"],
    )
    oracle = InternalTrafficPolicyMitigationOracle(problem)
    oracle.core_v1 = _CoreV1(policy)
    oracle.discovery_v1 = _DiscoveryV1(endpoints)
    oracle._connectivity_probe = lambda _node: True
    return oracle


def test_ready_endpoint_nodes_exclude_running_but_unready_backend():
    oracle = _oracle(
        [
            _endpoint("worker-a", False),
            _endpoint("worker-b", True),
        ]
    )

    assert oracle._nodes_with_ready_endpoint() == {"worker-b"}
    assert oracle.discovery_v1.requests == [("astronomy-shop", "kubernetes.io/service-name=recommendation")]


def test_local_policy_rejects_worker_with_only_unready_endpoint():
    oracle = _oracle(
        [
            _endpoint("worker-a", False),
            _endpoint("worker-b", True),
        ]
    )

    result = oracle.evaluate()

    assert result["success"] is False
    assert result["uncovered_nodes"] == ["worker-a"]


def test_local_policy_accepts_ready_endpoint_on_each_worker():
    oracle = _oracle(
        [
            _endpoint("worker-a", True),
            _endpoint("worker-b", True),
        ]
    )

    assert oracle.evaluate()["success"] is True


def test_cluster_policy_still_relies_on_connectivity_not_local_coverage():
    oracle = _oracle([_endpoint("worker-a", True)], policy="Cluster")

    assert oracle.evaluate()["success"] is True
