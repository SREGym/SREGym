from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.apps.slack_spine.resources import KubeletEvictionThresholdMisconfigSlack
from sregym.conductor.problems.kubelet_eviction_threshold_misconfig import KubeletEvictionThresholdMisconfig


@pytest.fixture(params=[KubeletEvictionThresholdMisconfig, KubeletEvictionThresholdMisconfigSlack])
def fault(request, tmp_path):
    problem = request.param.__new__(request.param)
    problem.namespace = "application"
    problem.faulty_service = "target"
    problem.target_node = "worker-a"
    problem.kubectl = Mock()
    problem.injector = Mock()
    problem.injector.inject_disk_pressure.return_value = 95
    problem.fault_injected = False
    problem._pick_worker_node = Mock(return_value="worker-a")
    problem.diagnosis_oracle = Mock()
    if isinstance(problem, KubeletEvictionThresholdMisconfigSlack):
        problem._state_path = Mock(return_value=tmp_path / "node")
    return problem


def test_pressure_starts_only_after_pinned_replicas_replace_old_ones(fault):
    operations = []
    fault.target_node = "before-deployment"
    fault.kubectl.exec_command_checked.side_effect = lambda command: operations.append(command)
    fault.injector.inject_disk_pressure.side_effect = lambda **kwargs: operations.append("pressure") or 95

    fault.inject_fault()

    assert len(operations) == 4
    assert operations[0].startswith("kubectl patch deployment target -n application")
    assert '"nodeName":"worker-a"' in operations[0]
    assert operations[1] == "kubectl rollout status deployment/target -n application --timeout=300s"
    assert operations[2] == "pressure"
    assert operations[3] == "kubectl wait node/worker-a --for=condition=DiskPressure=True --timeout=180s"
    assert fault.injected_threshold == 95
    assert fault.fault_injected is True
    assert fault.diagnosis_oracle.expected == fault.root_cause
    assert "worker-a" in str(fault.root_cause)
    assert "before-deployment" not in str(fault.root_cause)
    fault.kubectl.exec_command.assert_not_called()


@pytest.mark.parametrize("failed_step", [0, 1, 2])
def test_injection_command_failures_propagate(fault, failed_step):
    fault.kubectl.exec_command_checked.side_effect = [None] * failed_step + [RuntimeError("command failed")]

    with pytest.raises(RuntimeError, match="command failed"):
        fault.inject_fault()

    assert fault.fault_injected is False
    if failed_step < 2:
        fault.injector.inject_disk_pressure.assert_not_called()


def test_recovery_unpins_after_partial_injection(fault):
    fault.kubectl.exec_command_checked.side_effect = RuntimeError("pin failed")
    with pytest.raises(RuntimeError, match="pin failed"):
        fault.inject_fault()

    fault.recover_fault()

    fault.injector.recover_disk_pressure.assert_called_once_with(node_name="worker-a")
    assert "remove" in fault.kubectl.exec_command.call_args_list[0].args[0]
    assert "/spec/template/spec/nodeName" in fault.kubectl.exec_command.call_args_list[0].args[0]
    assert all("delete pods" not in call.args[0] for call in fault.kubectl.exec_command.call_args_list)


@pytest.mark.parametrize(
    "labels",
    [
        {"app": "locust-fetcher"},
        {"job": "workload"},
        {"app.kubernetes.io/component": "loadgen"},
        {"opentelemetry.io/name": "load-generator"},
    ],
)
def test_node_selection_protects_workload_helpers(fault, labels):
    del fault._pick_worker_node

    def pod(node, labels=None, volumes=None):
        return SimpleNamespace(
            metadata=SimpleNamespace(namespace="application", labels=labels),
            spec=SimpleNamespace(node_name=node, volumes=volumes),
        )

    fault.kubectl.core_v1_api.list_pod_for_all_namespaces.return_value.items = [
        pod("worker-a", labels),
        pod("worker-b", volumes=[SimpleNamespace(persistent_volume_claim=object())]),
    ]
    fault.kubectl.list_nodes.return_value.items = [
        SimpleNamespace(metadata=SimpleNamespace(name=name, labels=metadata))
        for name, metadata in [
            ("control-plane", {"node-role.kubernetes.io/control-plane": ""}),
            ("worker-a", {}),
            ("worker-b", {}),
            ("worker-c", {}),
        ]
    ]
    assert fault._pick_worker_node() == "worker-c"
    fault.kubectl.list_nodes.return_value.items = fault.kubectl.list_nodes.return_value.items[:2]
    with pytest.raises(RuntimeError, match="workload helpers"):
        fault._pick_worker_node()
