import json
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException
from urllib3.exceptions import MaxRetryError, NewConnectionError, ReadTimeoutError, SSLError

from sregym.conductor.conductor import Conductor
from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.calico_route_reflector_mitigation import CalicoRouteReflectorMitigationOracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.env_variable_shadowing_mitigation import EnvVariableShadowingMitigationOracle
from sregym.conductor.oracles.hpa_control_plane_mitigation import HPAControlPlaneMitigationOracle
from sregym.conductor.oracles.postgres_lock_mitigation import PostgresLockMitigationOracle
from sregym.conductor.oracles.priority_preemption_mitigation import PriorityPreemptionMitigationOracle


class RaisingOracle(Oracle):
    def evaluate(self, *args):
        raise self.problem.error


@pytest.mark.parametrize("route", ["caught", "compound", "mitigation", "diagnosis"])
@pytest.mark.parametrize(
    ("error", "reason", "failure_class"),
    [
        (ApiException(status=404), "kubernetes_resource_missing", "ambiguous"),
        (ApiException(status=403), "kubernetes_request_failed", "ambiguous"),
        (ApiException(status=422), "kubernetes_request_failed", "ambiguous"),
        (ApiException(status=0), "kubernetes_request_failed", "ambiguous"),
        (ApiException(status=503), "kubernetes_api_error", "environment_error"),
        (ApiException(status=429), "kubernetes_api_error", "environment_error"),
        (
            MaxRetryError(None, "/api", NewConnectionError(None, "connection refused")),
            "kubernetes_api_error",
            "environment_error",
        ),
        (ReadTimeoutError(None, "/api", "timed out"), "kubernetes_api_error", "environment_error"),
        (MaxRetryError(None, "/api", SSLError("certificate rejected")), "kubernetes_request_failed", "ambiguous"),
        (subprocess.CalledProcessError(1, "kubectl exec"), "oracle_command_failed", "ambiguous"),
        (subprocess.TimeoutExpired("kubectl exec", 30), "oracle_command_failed", "ambiguous"),
        (AttributeError("missing spec"), "oracle_raised", "harness_error"),
    ],
)
def test_exception_classification_is_consistent_across_evaluation_routes(route, error, reason, failure_class):
    oracle = RaisingOracle(SimpleNamespace(error=error))
    if route == "caught":
        result = oracle.fail_from_exception(error)
    elif route == "compound":
        result = CompoundedOracle(None, oracle).evaluate()
    else:
        conductor = Conductor.__new__(Conductor)
        conductor.problem = SimpleNamespace(mitigation_oracle=oracle, diagnosis_oracle=oracle)
        conductor.logger = logging.getLogger(__name__)
        conductor.execution_start_time = 0
        result = getattr(conductor, f"_evaluate_{route}")("answer")
        assert type(error).__name__ in result["error"]
    assert result["success"] is False
    assert result["reason"] == reason
    assert result["failure_class"] == failure_class


def test_checked_command_preserves_its_failure_cause():
    error = RuntimeError("checked command failed")
    error.__cause__ = subprocess.CalledProcessError(1, "kubectl exec")
    result = RaisingOracle.fail_from_exception(error)
    assert result["reason"] == "oracle_command_failed"
    assert result["failure_class"] == "ambiguous"


@pytest.mark.parametrize(
    ("cls", "checks"),
    [
        (
            EnvVariableShadowingMitigationOracle,
            ["_host_configuration_unsafe", "_service_target_endpoint_unready"],
        ),
        (
            CalicoRouteReflectorMitigationOracle,
            ["_app_replicas_were_reduced", "_deployments_unready", "_application_not_spanning_nodes"],
        ),
        (
            CalicoRouteReflectorMitigationOracle,
            ["_bgp_configuration_not_route_reflector_mode", "_route_reflector_peer_selects_no_nodes"],
        ),
        (
            PriorityPreemptionMitigationOracle,
            ["_service_endpoint_unready", "_any_deployment_unready", "_any_app_pod_unready"],
        ),
    ],
)
def test_failed_check_prevents_later_checks_from_running(cls, checks, monkeypatch):
    oracle = cls.__new__(cls)
    oracle.problem = SimpleNamespace(
        namespace="app",
        faulty_service="frontend",
        PROBE_NAMESPACE="probes",
        PRESSURE_NAMESPACE="pressure",
        PRESSURE_DEPLOYMENT="worker",
        PLATFORM_PRIORITY_CLASS="platform",
        kubectl=Mock(),
    )
    for name in (
        "_wait_for_rollouts",
        "_app_replicas_were_reduced",
        "_deployments_unready",
        "_application_not_spanning_nodes",
        "_bgp_configuration_not_route_reflector_mode",
        "_route_reflector_peer_selects_no_nodes",
    ):
        if hasattr(oracle, name):
            monkeypatch.setattr(oracle, name, Mock(return_value=None))
    if cls is EnvVariableShadowingMitigationOracle:
        monkeypatch.setattr(oracle, "_desired_replicas", Mock(return_value=1))
        monkeypatch.setattr(oracle, "_wait_for_current_rollout", Mock(return_value=Mock()))
        monkeypatch.setattr(oracle, "_find_container", Mock(return_value=Mock()))
    elif cls is PriorityPreemptionMitigationOracle:
        monkeypatch.setattr(oracle, "_deployment_unready", Mock(return_value=(None, Mock())))
    else:
        monkeypatch.setattr(oracle, "_calico_ready", Mock(return_value=True))
    failure = oracle.fail("fault_still_present")
    monkeypatch.setattr(oracle, checks[0], Mock(return_value=failure))
    later = Mock(side_effect=ApiException(status=404))
    monkeypatch.setattr(oracle, checks[1], later)
    assert oracle.evaluate() == failure
    later.assert_not_called()


@pytest.mark.parametrize(
    "check", ["_bgp_configuration_not_route_reflector_mode", "_route_reflector_peer_selects_no_nodes"]
)
@pytest.mark.parametrize("returncode", [1, 124])
def test_calico_command_failure_does_not_claim_a_missing_resource(check, returncode):
    oracle = CalicoRouteReflectorMitigationOracle.__new__(CalicoRouteReflectorMitigationOracle)
    oracle._run = Mock(return_value=subprocess.CompletedProcess("kubectl", returncode, "", "request failed"))

    result = getattr(oracle, check)()

    assert result["reason"] == "oracle_command_failed"
    assert result["failure_class"] == "ambiguous"
    assert result["detail"]["stderr"] == "request failed"


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", "bgp_configuration_missing"),
        ("\n", "bgp_configuration_missing"),
        (json.dumps({"spec": {"nodeToNodeMeshEnabled": True}}), "node_to_node_mesh_enabled"),
        (json.dumps({"spec": {"nodeToNodeMeshEnabled": False}}), None),
    ],
)
def test_calico_configuration_absence_is_confirmed_by_a_successful_request(stdout, reason):
    oracle = CalicoRouteReflectorMitigationOracle.__new__(CalicoRouteReflectorMitigationOracle)
    oracle._run = Mock(return_value=subprocess.CompletedProcess("kubectl", 0, stdout, ""))

    result = oracle._bgp_configuration_not_route_reflector_mode()

    assert "--ignore-not-found" in oracle._run.call_args.args[0]
    if reason is None:
        assert result is None
    else:
        assert result["success"] is False
        assert result["reason"] == reason
        assert result["failure_class"] == "agent_error"


def _hpa_with_clock(monkeypatch, timeout=2):
    clock = SimpleNamespace(now=0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr("sregym.conductor.oracles.hpa_control_plane_mitigation.time.monotonic", lambda: clock.now)
    monkeypatch.setattr("sregym.conductor.oracles.hpa_control_plane_mitigation.time.sleep", sleep)
    return HPAControlPlaneMitigationOracle(
        SimpleNamespace(namespace="app", kubectl=Mock()),
        deployment_name="frontend",
        hpa_name="frontend-capacity",
        timeout_seconds=timeout,
        poll_interval_seconds=1,
    )


@pytest.mark.parametrize(
    ("failure", "reason", "category"),
    [
        (subprocess.CalledProcessError(1, "kubectl"), "oracle_command_failed", "ambiguous"),
        (subprocess.TimeoutExpired("kubectl", 30), "oracle_command_failed", "ambiguous"),
        (ApiException(status=503), "kubernetes_api_error", "environment_error"),
    ],
)
def test_hpa_keeps_observation_failure_after_polling(monkeypatch, failure, reason, category):
    oracle = _hpa_with_clock(monkeypatch)
    oracle.problem.kubectl.exec_command_checked.side_effect = failure

    result = oracle.evaluate()

    assert result["reason"] == reason
    assert result["failure_class"] == category
    assert oracle.problem.kubectl.exec_command_checked.call_count == 3
    assert oracle.problem.kubectl.exec_command_checked.call_args.kwargs["timeout"] == 30
    json.dumps(result)


@pytest.mark.parametrize("output", ["", "not json", "null", "[]"])
def test_hpa_unreadable_response_is_not_an_unhealthy_hpa(monkeypatch, output):
    oracle = _hpa_with_clock(monkeypatch, timeout=0)
    oracle.problem.kubectl.exec_command_checked.return_value = output

    result = oracle.evaluate()

    assert result["reason"] == "hpa_observation_failed"
    assert result["failure_class"] == "ambiguous"


@pytest.mark.parametrize(
    ("polls", "success", "reason"),
    [
        ([RuntimeError("read failed"), (True, "ready"), (True, "ready")], True, None),
        (
            [RuntimeError("read failed"), (False, "missing CPU request"), (False, "missing CPU request")],
            False,
            "hpa_never_became_healthy",
        ),
        ([(True, "ready"), subprocess.TimeoutExpired("kubectl", 30), (True, "ready")], False, "oracle_command_failed"),
        ([(False, "missing CPU request"), (True, "ready"), (True, "ready")], True, None),
    ],
)
def test_hpa_transient_errors_do_not_bypass_consecutive_healthy_polls(monkeypatch, polls, success, reason):
    oracle = _hpa_with_clock(monkeypatch)
    oracle._evaluate_once = Mock(side_effect=polls)

    result = oracle.evaluate()

    assert result["success"] is success
    assert result.get("reason") == reason


@pytest.mark.parametrize("failure_at", [0, 1, 2])
def test_hpa_nested_observations_propagate_command_errors(monkeypatch, failure_at):
    oracle = _hpa_with_clock(monkeypatch, timeout=0)
    deployment = {
        "metadata": {"generation": 1},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "frontend"}}},
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    pods = {"items": [{"metadata": {"name": "frontend-1"}, "status": {"phase": "Running"}}]}
    responses = [json.dumps(deployment), json.dumps(pods), '{"items": []}']
    responses[failure_at] = subprocess.CalledProcessError(1, "kubectl")
    oracle.problem.kubectl.exec_command_checked.side_effect = responses

    result = oracle.evaluate()

    assert result["reason"] == "oracle_command_failed"


@pytest.mark.parametrize(
    ("read_status", "api_ok", "reason"),
    [
        ("locked", True, "fault_still_present"),
        ("other", True, "catalog_read_failed"),
        ("ok", False, "catalog_not_reachable"),
        ("ok", True, None),
    ],
)
def test_postgres_lock_verdict_distinguishes_read_status(monkeypatch, read_status, api_ok, reason):
    problem = SimpleNamespace(_catalog_read_status=Mock(return_value=read_status))
    oracle = PostgresLockMitigationOracle(problem)
    oracle.SUSTAINED_SECONDS = 0
    oracle._product_catalog_available = Mock(return_value=api_ok)

    result = oracle.evaluate()

    assert result["success"] is (reason is None)
    assert result.get("reason") == reason
    if reason is not None:
        assert result["failure_class"] == ("agent_error" if read_status == "locked" else "ambiguous")
    assert oracle._product_catalog_available.call_count == (1 if read_status == "ok" else 0)
