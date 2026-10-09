import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def validator():
    path = Path(__file__).parent / "integration" / "validate_problem.py"
    spec = importlib.util.spec_from_file_location("lite_validator_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conductor(monkeypatch, validator):
    problem = Mock()
    problem.namespace = "test-app"
    conductor = Mock()
    conductor._get_verifier_runtime.return_value.evaluate.side_effect = [
        {"success": True},
        {"success": False},
        {"success": True},
    ]
    conductor.problems.PROBLEM_REGISTRY = {"problem": lambda: problem}
    conductor.problems.get_problem_instance.return_value = problem
    monkeypatch.setattr(validator, "Conductor", Mock(return_value=conductor))
    return conductor, problem


def test_success_requires_oracle_transition_and_cleanup(monkeypatch, validator):
    conductor, problem = _conductor(monkeypatch, validator)
    passed, stages = validator.validate("problem", 0, 0, 0, deploy_loki=True)
    assert passed
    assert all(stage.status == validator.PASS for stage in stages.values())
    assert validator.Conductor.call_args.kwargs["config"].deploy_loki
    problem.mitigation_oracle.capture_baseline.assert_called_once()
    problem.mitigation_oracle.evaluate.assert_not_called()
    conductor._get_verifier_runtime.return_value.prepare.assert_called_once()
    conductor._get_verifier_runtime.return_value.cancel.assert_called_once()
    problem.inject_fault.assert_called_once()
    problem.recover_fault.assert_called_once()
    problem.app.cleanup.assert_called_once()
    problem.stop_environment.assert_called_once()
    conductor.kubectl.wait_for_namespace_deletion.assert_called_once_with("test-app")
    conductor.mcp_server.stop_port_forward.assert_called_once()
    conductor.stop_k8s_proxy.assert_called_once()


def test_partial_injection_attempts_recovery_before_cleanup(monkeypatch, validator):
    conductor, problem = _conductor(monkeypatch, validator)
    problem.inject_fault.side_effect = RuntimeError("partially injected")
    passed, stages = validator.validate("problem", 0, 0, 0)
    assert not passed
    assert stages["inject"].status == validator.FAIL
    problem.recover_fault.assert_called_once()
    problem.app.cleanup.assert_called_once()
    conductor.mcp_server.stop_port_forward.assert_called_once()


def test_cleanup_failure_is_not_reported_as_a_pass(monkeypatch, validator):
    conductor, problem = _conductor(monkeypatch, validator)
    problem.app.cleanup.side_effect = RuntimeError("namespace stuck")
    passed, stages = validator.validate("problem", 0, 0, 0)
    assert not passed
    assert stages["cleanup"].status == validator.FAIL
    conductor.mcp_server.stop_port_forward.assert_called_once()


def test_oracle_exception_does_not_count_as_detecting_fault(validator):
    oracle = Mock()
    runtime = Mock()
    runtime.evaluate.side_effect = RuntimeError("probe cannot run")
    matched, checks, result = validator._poll_oracle(runtime, oracle, False, 0, 0)
    assert not matched
    assert checks == 1
    assert result["success"] is None


@pytest.mark.parametrize("verdict", [{"success": False, "failure_class": "harness_error"}, {"success": 0}, {}])
def test_harness_failure_and_malformed_verdict_do_not_count_as_a_live_fault(validator, verdict):
    runtime = Mock()
    runtime.evaluate.return_value = verdict
    matched, checks, _ = validator._poll_oracle(runtime, Mock(), False, 30, 0)
    assert not matched
    assert checks == 1


def test_unhealthy_setup_is_rejected_before_fault_injection(monkeypatch, validator):
    conductor, problem = _conductor(monkeypatch, validator)
    conductor._get_verifier_runtime.return_value.evaluate.return_value = {"success": False}
    conductor._get_verifier_runtime.return_value.evaluate.side_effect = None
    passed, stages = validator.validate("problem", 0, 0, 0)
    assert not passed
    assert stages["deploy"].status == validator.FAIL
    problem.inject_fault.assert_not_called()
    conductor._get_verifier_runtime.return_value.cancel.assert_called_once()
    problem.stop_environment.assert_called_once()


def test_verifier_preparation_failure_has_no_host_fallback(monkeypatch, validator):
    conductor, problem = _conductor(monkeypatch, validator)
    conductor._get_verifier_runtime.return_value.prepare.side_effect = RuntimeError("container unavailable")
    passed, stages = validator.validate("problem", 0, 0, 0)
    assert not passed
    assert stages["deploy"].status == validator.FAIL
    problem.inject_fault.assert_not_called()
    problem.mitigation_oracle.evaluate.assert_not_called()
    conductor._get_verifier_runtime.return_value.cancel.assert_called_once()


def test_validation_noise_is_opt_in(monkeypatch, validator):
    _conductor(monkeypatch, validator)
    passed, _ = validator.validate("problem", 0, 0, 0, enable_noise=True)
    assert passed
    assert validator.Conductor.call_args.kwargs["config"].enable_noise is True


def test_failed_recovery_still_cleans_up_application_and_proxies(monkeypatch, validator):
    conductor, problem = _conductor(monkeypatch, validator)
    problem.inject_fault.side_effect = RuntimeError("partially injected")
    problem.recover_fault.side_effect = RuntimeError("cannot recover")
    passed, stages = validator.validate("problem", 0, 0, 0)
    assert not passed
    assert stages["cleanup"].status == validator.FAIL
    problem.app.cleanup.assert_called_once()
    conductor.mcp_server.stop_port_forward.assert_called_once()
    conductor.stop_k8s_proxy.assert_called_once()


def test_failed_port_forward_cleanup_still_stops_proxy(monkeypatch, validator):
    conductor, problem = _conductor(monkeypatch, validator)
    conductor.mcp_server.stop_port_forward.side_effect = RuntimeError("cannot stop")
    passed, stages = validator.validate("problem", 0, 0, 0)
    assert not passed
    assert stages["cleanup"].status == validator.FAIL
    conductor.stop_k8s_proxy.assert_called_once()
