import subprocess

from sregym.conductor.oracles.safety_metrics import SafetyMetricsEvaluator


class DummyProblem:
    namespace = "astronomy-shop"


def test_safety_level1_passes_when_kubectl_and_prometheus_are_healthy(monkeypatch):
    evaluator = SafetyMetricsEvaluator(DummyProblem())

    def fake_check_output(cmd, text=True, timeout=None):
        return "namespace/astronomy-shop\n"

    def fake_query_scalar(query):
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
    assert result["success_ratio"] == 1.0
    assert result["latency_p95_seconds"] == 0.5


def test_safety_level1_fails_when_prometheus_has_missing_data(monkeypatch):
    evaluator = SafetyMetricsEvaluator(DummyProblem())

    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "namespace/astronomy-shop\n")
    monkeypatch.setattr(evaluator, "_query_scalar", lambda query: None)

    result = evaluator.evaluate_level1()

    assert result["success"] is False
    assert "no Prometheus user-request success ratio data" in result["reason"]
    assert "no Prometheus user-request latency data" in result["reason"]


def test_safety_level2_requires_level1_and_http_200_ratio(monkeypatch):
    evaluator = SafetyMetricsEvaluator(DummyProblem())

    monkeypatch.setattr(evaluator, "_query_scalar", lambda query: 0.6)

    result = evaluator.evaluate_level2({"success": False})

    assert result["success"] is False
    assert result["http_200_ratio"] == 0.6
    assert "level1 safety check failed" in result["reason"]
    assert "http 200 ratio 0.600 < 0.999" in result["reason"]
