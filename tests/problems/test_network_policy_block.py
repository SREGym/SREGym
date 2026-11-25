"""network_policy_block must break requests over connections opened before the fault."""

from types import SimpleNamespace

import pytest

from sregym.conductor.problems.network_policy_block import NetworkPolicyBlock


class _Recorder:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    # NetworkingV1Api
    def create_namespaced_network_policy(self, namespace, body):
        self.calls.append(("policy", namespace, body))

    # KubeCtl
    def exec_command_checked(self, command, input_data=None, timeout=None):
        self.calls.append(("kubectl", command))
        if self.fail_on and self.fail_on in command:
            raise RuntimeError(f"kubectl failed: {command}")
        return ""


def _problem(client_deployment="frontend", fail_on=None):
    recorder = _Recorder(fail_on=fail_on)
    problem = object.__new__(NetworkPolicyBlock)
    problem.namespace = "hotel-reservation"
    problem.faulty_service = "recommendation"
    problem.client_deployment = client_deployment
    problem.policy_name = "deny-all-recommendation"
    problem.networking_v1 = recorder
    problem.kubectl = recorder
    problem.app = SimpleNamespace()
    return problem, recorder


def test_denies_all_traffic_then_restarts_the_frontend():
    """The frontend reuses the gRPC channel it opened at startup, which the policy
    never touches. Restarting it after the policy exists forces a new connection,
    which the policy blocks."""
    problem, recorder = _problem()

    problem.inject_fault()

    (kind, namespace, policy), restart, status = recorder.calls
    assert kind == "policy" and namespace == "hotel-reservation"
    assert policy["spec"]["podSelector"] == {"matchLabels": {"io.kompose.service": "recommendation"}}
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert policy["spec"]["ingress"] == [] and policy["spec"]["egress"] == []
    assert restart == ("kubectl", "kubectl rollout restart deployment/frontend -n hotel-reservation")
    assert status[1].startswith("kubectl rollout status deployment/frontend -n hotel-reservation ")
    assert problem.fault_injected is True


def test_restarts_the_configured_client():
    problem, recorder = _problem(client_deployment="gateway")

    problem.inject_fault()

    assert ("kubectl", "kubectl rollout restart deployment/gateway -n hotel-reservation") in recorder.calls


def test_failed_restart_fails_injection():
    """Without the restart the policy breaks no request, so injection must not report success."""
    problem, _ = _problem(fail_on="rollout status")
    problem.fault_injected = False

    with pytest.raises(RuntimeError, match="rollout status"):
        problem.inject_fault()
    assert problem.fault_injected is False
