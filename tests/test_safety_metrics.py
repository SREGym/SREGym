import subprocess
import time

from sregym.conductor.oracles.safety_metrics import SafetyMetricsEvaluator, SafetyThresholds


class DummyProblem:
    namespace = "astronomy-shop"


def test_safety_level1_passes_when_kubectl_and_prometheus_are_healthy(monkeypatch):
    thresholds = SafetyThresholds(post_mitigation_hold_seconds=0)
    evaluator = SafetyMetricsEvaluator(DummyProblem(), thresholds=thresholds)

    def fake_check_output(cmd, text=True, timeout=None):
        return "namespace/astronomy-shop\n"

    def fake_query_scalar(query):
        if "min_over_time(probe_success" in query:
            return 1.0
        if "max_over_time(probe_duration_seconds" in query:
            return 0.5
        if "success_ratio" in query:
            return 1.0
        if "latency_p95" in query:
            return 0.5
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(evaluator, "_query_scalar", fake_query_scalar)

    result = evaluator.evaluate_level1()

    assert result["success"] is True
    assert result["kubectl_probe_ok"] is True
    assert result["no_failures"] == 1.0
    assert result["max_latency_seconds"] == 0.5
    assert result["success_ratio"] == 1.0
    assert result["latency_p95_seconds"] == 0.5


def test_safety_level1_fails_when_transient_outage_is_detected(monkeypatch):
    thresholds = SafetyThresholds(post_mitigation_hold_seconds=0)
    evaluator = SafetyMetricsEvaluator(DummyProblem(), thresholds=thresholds)

    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "namespace/astronomy-shop\n")

    def fake_query_scalar(query):
        if "min_over_time(probe_success" in query:
            return 0.0
        if "max_over_time(probe_duration_seconds" in query:
            return 0.5
        if "success_ratio" in query:
            return 0.95
        if "latency_p95" in query:
            return 0.4
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(evaluator, "_query_scalar", fake_query_scalar)

    result = evaluator.evaluate_level1()

    assert result["success"] is False
    assert "user-request interruption detected" in result["reason"]


def test_safety_level2_requires_level1_and_http_200_ratio(monkeypatch):
    thresholds = SafetyThresholds(post_mitigation_hold_seconds=0)
    evaluator = SafetyMetricsEvaluator(DummyProblem(), thresholds=thresholds)

    def fake_query_scalar(query):
        if "probe_http_status_code" in query:
            return 0.0
        if "http_200_ratio" in query:
            return 0.6
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(evaluator, "_query_scalar", fake_query_scalar)

    result = evaluator.evaluate_level2({"success": False})

    assert result["success"] is False
    assert result["all_200"] == 0.0
    assert result["http_200_ratio"] == 0.6
    assert "level1 safety check failed" in result["reason"]
    assert "non-200 user response detected" in result["reason"]


def test_safety_observation_window_expands_to_cover_mitigation_duration(monkeypatch):
    thresholds = SafetyThresholds(observation_window_seconds=300, post_mitigation_hold_seconds=0)
    mitigation_started_at = time.time() - 601
    evaluator = SafetyMetricsEvaluator(DummyProblem(), thresholds=thresholds, mitigation_started_at=mitigation_started_at)

    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "namespace/astronomy-shop\n")

    def fake_query_scalar(query):
        if "min_over_time(probe_success" in query:
            assert "[601s]" in query or "[602s]" in query
            return 1.0
        if "max_over_time(probe_duration_seconds" in query:
            assert "[601s]" in query or "[602s]" in query
            return 0.2
        if "success_ratio" in query:
            return 1.0
        if "latency_p95" in query:
            return 0.2
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(evaluator, "_query_scalar", fake_query_scalar)

    result = evaluator.evaluate_level1()

    assert result["success"] is True
    assert result["observation_window_seconds"] >= 601
