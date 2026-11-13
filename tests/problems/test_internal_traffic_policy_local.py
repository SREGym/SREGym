from unittest.mock import Mock

import pytest

from sregym.conductor.problems.apps.slack_spine.networking import InternalTrafficPolicyLocalIA
from sregym.conductor.problems.internal_traffic_policy_local import InternalTrafficPolicyLocalAstronomyShop


@pytest.fixture(params=[InternalTrafficPolicyLocalAstronomyShop, InternalTrafficPolicyLocalIA])
def fault(request):
    problem = request.param.__new__(request.param)
    problem.namespace = "application"
    problem.fault_injected = False
    problem._select_nodes = Mock(return_value=("worker-a", "worker-b"))
    problem._pin_deployment_to_node = Mock()
    problem._wait_for_pod_on_node = Mock()
    problem._set_internal_traffic_policy = Mock()
    problem.kubectl = Mock()
    return problem


def test_refreshes_backend_connections_after_enabling_local_policy(fault):
    operations = []
    fault._set_internal_traffic_policy.side_effect = lambda policy: operations.append(("policy", policy))
    fault.kubectl.exec_command_checked.side_effect = lambda command: operations.append(("command", command))

    fault.inject_fault()

    restart = f"kubectl rollout restart deployment/{fault.FAULTY_SERVICE} -n application"
    status = f"kubectl rollout status deployment/{fault.FAULTY_SERVICE} -n application --timeout=300s"
    assert operations[operations.index(("policy", "Local")) + 1 :] == [
        ("command", restart),
        ("command", status),
    ]
    assert fault._wait_for_pod_on_node.call_args_list[-1].args == ("worker-a",)
    assert fault.kubectl.exec_command.call_count == 0
    assert fault.fault_injected is True


@pytest.mark.parametrize("failure", ["restart", "status"])
def test_connection_refresh_errors_do_not_report_successful_injection(fault, failure):
    active = False

    def set_policy(policy):
        nonlocal active
        active = policy == "Local"

    def checked(command):
        if active and f"rollout {failure}" in command:
            raise RuntimeError("backend refresh failed")

    fault._set_internal_traffic_policy.side_effect = set_policy
    fault.kubectl.exec_command_checked.side_effect = checked

    with pytest.raises(RuntimeError, match="backend refresh failed"):
        fault.inject_fault()
    assert fault.fault_injected is False
