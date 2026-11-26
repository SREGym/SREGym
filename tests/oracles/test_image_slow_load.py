from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles import image_slow_load as oracle_module
from sregym.conductor.problems import image_slow_load as problem_module
from sregym.generators.workload.image_client import ImageEvidenceError


@pytest.mark.parametrize(
    ("reason", "classification"),
    [
        ("fault_still_present", "agent_error"),
        ("image_measurement_unavailable", "environment_error"),
        ("image_evidence_missing", "environment_error"),
        ("image_request_failed", "ambiguous"),
    ],
)
def test_failed_image_verification_does_not_pass(monkeypatch, reason, classification):
    wait = Mock(side_effect=ImageEvidenceError(reason, "real image check did not pass"))
    monkeypatch.setattr(oracle_module, "wait_for_images", wait)
    result = oracle_module.ImageSlowLoadOracle(SimpleNamespace(kubectl=Mock())).evaluate()
    assert result["success"] is False
    assert result["reason"] == reason
    assert result["failure_class"] == classification


def test_fresh_fast_images_pass_without_requiring_an_exact_flag_edit(monkeypatch):
    wait = Mock(return_value="20 image responses, latency 0.021–0.082s")
    monkeypatch.setattr(oracle_module, "wait_for_images", wait)
    monkeypatch.setattr(oracle_module, "time", SimpleNamespace(time=lambda: 101))
    problem = SimpleNamespace(kubectl=Mock())
    assert oracle_module.ImageSlowLoadOracle(problem).evaluate()["success"] is True
    wait.assert_called_once_with(problem.kubectl, after=101, timeout=120)


@pytest.mark.parametrize(("service", "expected"), [("accounting", False), ("frontend-proxy", True)])
def test_handoff_only_accepts_the_frontend_proxy_latency_alert(monkeypatch, service, expected):
    query = Mock(return_value=[{"labels": {"alertname": "HighRequestLatency", "service_name": service}}])
    monkeypatch.setattr(problem_module, "AlertOracle", lambda _: SimpleNamespace(_query_firing_alerts=query))
    problem = problem_module.ImageSlowLoad.__new__(problem_module.ImageSlowLoad)
    problem.namespace = "astronomy-shop"
    assert problem._frontend_latency_alert_is_firing() is expected
    query.assert_called_once_with("astronomy-shop")


def test_injection_cannot_hand_off_before_the_image_impact_check_passes(monkeypatch):
    problem = problem_module.ImageSlowLoad.__new__(problem_module.ImageSlowLoad)
    problem.kubectl = Mock()
    problem.injector = Mock()
    problem.feature_flag = "imageSlowLoad"
    problem.fault_injected = False
    guard = Mock(side_effect=ImageEvidenceError("image_latency_alert_missing", "never fired"))
    monkeypatch.setattr(problem_module, "wait_for_images", guard)
    with pytest.raises(ImageEvidenceError, match="never fired"):
        problem.inject_fault()
    problem.injector.inject_fault.assert_called_once_with("imageSlowLoad")
    assert guard.call_args.kwargs["delayed"] is True
    assert guard.call_args.kwargs["alert_is_firing"] == problem._frontend_latency_alert_is_firing
    assert problem.fault_injected is False
