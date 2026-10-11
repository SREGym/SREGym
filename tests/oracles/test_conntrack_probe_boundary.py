from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.conntrack_mitigation import ConntrackMitigationOracle
from sregym.service.agent_visibility_policy import (
    NODE_MAINTENANCE_LABEL,
    NODE_MAINTENANCE_ROLE,
    is_hidden_resource,
)


@pytest.mark.parametrize("phase,passed", [("Succeeded", True), ("Failed", False), ("Pending", False)])
def test_functional_probe_is_protected_without_changing_workload_dns(phase, passed):
    oracle = object.__new__(ConntrackMitigationOracle)
    oracle.problem = SimpleNamespace(namespace="customer-app")
    oracle.probe_attempts = 3
    oracle.core_v1 = Mock()
    oracle.core_v1.read_namespaced_pod_log.return_value = "PROBE_OK=3 PROBE_FAIL=0"
    oracle._wait_for_probe_pod = Mock(return_value=phase)
    assert oracle._frontend_probe_succeeds("node-1") is passed
    namespace, pod = oracle.core_v1.create_namespaced_pod.call_args.args
    assert namespace == "customer-app"
    assert pod["metadata"]["labels"][NODE_MAINTENANCE_LABEL] == NODE_MAINTENANCE_ROLE
    assert len(pod["metadata"]["name"].removeprefix("service-healthcheck-")) == 32
    assert "http://frontend:5000/" in pod["spec"]["containers"][0]["command"][-1]
    assert pod["spec"]["nodeName"] == "node-1"
    assert is_hidden_resource(pod, set(), {})
    oracle.core_v1.delete_namespaced_pod.assert_called_once_with(
        pod["metadata"]["name"], namespace, grace_period_seconds=0
    )


def test_probe_creation_conflict_never_deletes_an_unowned_pod():
    oracle = object.__new__(ConntrackMitigationOracle)
    oracle.problem = SimpleNamespace(namespace="customer-app")
    oracle.probe_attempts = 3
    oracle.core_v1 = Mock()
    oracle.core_v1.create_namespaced_pod.side_effect = ApiException(status=409)
    assert not oracle._frontend_probe_succeeds("node-1")
    oracle.core_v1.delete_namespaced_pod.assert_not_called()
