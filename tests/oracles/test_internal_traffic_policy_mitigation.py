from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

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
        self.services = {
            name: client.V1Service(metadata=client.V1ObjectMeta(name=name), spec=client.V1ServiceSpec())
            for name in ("recommendation", "frontend")
        }

    def read_namespaced_service(self, name, namespace):
        service = self.services[name]
        service.spec.internal_traffic_policy = self.policy
        return service

    def list_namespaced_service(self, namespace):
        return SimpleNamespace(items=list(self.services.values()))

    def list_namespaced_persistent_volume_claim(self, namespace):
        return SimpleNamespace(items=[])


class _DiscoveryV1:
    def __init__(self, endpoints):
        self.endpoints = endpoints
        self.requests = []

    def list_namespaced_endpoint_slice(self, namespace, label_selector):
        self.requests.append((namespace, label_selector))
        return SimpleNamespace(items=[SimpleNamespace(endpoints=self.endpoints)])


def _oracle(endpoints, policy="Local"):
    deployments = {name: _deployment(name) for name in ("recommendation", "frontend")}
    core = _CoreV1(policy)
    kubectl = SimpleNamespace(
        deployments=deployments,
        list_deployments=lambda _: SimpleNamespace(items=list(deployments.values())),
        list_pods=lambda _: SimpleNamespace(
            items=[
                client.V1Pod(
                    metadata=client.V1ObjectMeta(name="frontend-pod"),
                    spec=client.V1PodSpec(containers=[]),
                    status=client.V1PodStatus(phase="Running", container_statuses=[]),
                )
            ]
        ),
        core_v1_api=core,
        apps_v1_api=SimpleNamespace(list_namespaced_stateful_set=lambda _: SimpleNamespace(items=[])),
    )
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        FAULTY_SERVICE="recommendation",
        SERVICE_PORT=8080,
        victim_node="worker-b",
        worker_nodes=lambda: ["worker-a", "worker-b"],
        kubectl=kubectl,
    )
    oracle = InternalTrafficPolicyMitigationOracle(problem)
    oracle.core_v1 = core
    oracle.discovery_v1 = _DiscoveryV1(endpoints)
    oracle._connectivity_probe = Mock(return_value=True)
    oracle.capture_baseline()
    oracle.rollout_time = 0
    return oracle


def _deployment(name, replicas=1):
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, generation=1),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(spec=client.V1PodSpec(containers=[])),
        ),
        status=client.V1DeploymentStatus(
            observed_generation=1,
            replicas=replicas,
            updated_replicas=replicas,
            ready_replicas=replicas,
            available_replicas=replicas,
        ),
    )


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


@pytest.mark.parametrize("name", ["frontend", "recommendation"])
def test_rejects_required_deployment_scaled_to_zero_before_network_probe(name):
    oracle = _oracle([_endpoint("worker-a", True)], policy="Cluster")
    oracle.problem.kubectl.deployments[name] = _deployment(name, replicas=0)

    result = oracle.evaluate()

    assert result["reason"] == "required_deployment_scaled_to_zero"
    assert result["failure_class"] == "agent_error"
    assert result["detail"]["deployment"] == name
    oracle._connectivity_probe.assert_not_called()


def test_rejects_deleted_caller_before_network_probe():
    oracle = _oracle([_endpoint("worker-a", True)], policy="Cluster")
    del oracle.problem.kubectl.deployments["frontend"]

    result = oracle.evaluate()

    assert result["reason"] == "required_deployment_missing"
    assert result["failure_class"] == "agent_error"
    oracle._connectivity_probe.assert_not_called()


def test_rejects_deleted_caller_service_before_network_probe():
    oracle = _oracle([_endpoint("worker-a", True)], policy="Cluster")
    del oracle.core_v1.services["frontend"]

    result = oracle.evaluate()

    assert result["reason"] == "required_resource_missing"
    assert result["failure_class"] == "agent_error"
    oracle._connectivity_probe.assert_not_called()


def test_rejects_stale_caller_rollout():
    oracle = _oracle([_endpoint("worker-a", True)], policy="Cluster")
    oracle.problem.kubectl.deployments["frontend"].metadata.generation = 2

    assert oracle.evaluate()["reason"] == "deployment_replicas_unready"
    oracle._connectivity_probe.assert_not_called()


def test_accepts_healthy_replica_changes():
    oracle = _oracle([_endpoint("worker-a", True)], policy="Cluster")
    oracle.problem.kubectl.deployments["frontend"] = _deployment("frontend", replicas=3)

    assert oracle.evaluate()["success"] is True


def test_shared_health_does_not_replace_network_probe():
    oracle = _oracle([_endpoint("worker-a", True)], policy="Cluster")
    oracle._connectivity_probe.return_value = False

    assert oracle.evaluate()["reason"] == "internal_traffic_probe_failed"
    oracle._connectivity_probe.assert_called_once_with("worker-b")


def test_removed_policy_field_keeps_cluster_behavior():
    oracle = _oracle([_endpoint("worker-a", True)], policy=None)

    assert oracle.evaluate()["success"] is True


def test_shared_api_failure_is_not_reported_as_health(monkeypatch):
    oracle = _oracle([_endpoint("worker-a", True)], policy="Cluster")

    def unavailable(namespace):
        raise ApiException(status=503)

    monkeypatch.setattr(oracle.core_v1, "list_namespaced_service", unavailable)
    with pytest.raises(ApiException, match="503"):
        oracle.evaluate()
    oracle._connectivity_probe.assert_not_called()
