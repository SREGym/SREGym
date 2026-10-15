from types import SimpleNamespace

import pytest

from sregym.chaos import protected, slo
from sregym.conductor.oracles.slo_oracle import SLOMitigationOracle


@pytest.fixture
def oracle(monkeypatch):
    monkeypatch.setattr(protected, "snapshot", lambda _: {})
    app = SimpleNamespace(namespace="app", wrk=SimpleNamespace(wrk=SimpleNamespace(rate=100)))
    return SLOMitigationOracle(SimpleNamespace(app=app), buffer_seconds=0)


def test_baseline_requires_latency(oracle, monkeypatch):
    rounds = [slo.RoundStats(time=i, requests=3000, duration_s=30) for i in range(4)]
    monkeypatch.setattr(slo, "collect_rounds", lambda *a, **kw: rounds)
    with pytest.raises(RuntimeError, match="latency measurements"):
        oracle.capture_baseline()


def test_recovery_without_latency_is_incomplete(oracle, monkeypatch):
    oracle.slo = slo.SLO(target_rps=100)
    rounds = [slo.RoundStats(time=i, requests=3000, duration_s=30) for i in range(4)]
    monkeypatch.setattr(slo, "collect_rounds", lambda *a, **kw: rounds)
    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "slo_incomplete_measurement"


@pytest.mark.parametrize("changes", [{"non2xx": 3000}, {"timeouts": 3000}, {"p99_ms": 5000}, {"requests": 100}])
def test_broken_baseline_cannot_calibrate_itself_healthy(oracle, monkeypatch, changes):
    rounds = [
        slo.RoundStats(**{"time": i, "requests": 3000, "duration_s": 30, "p99_ms": 10, **changes}) for i in range(4)
    ]
    monkeypatch.setattr(slo, "collect_rounds", lambda *a, **kw: rounds)
    with pytest.raises(RuntimeError, match="does not meet the baseline SLO"):
        oracle.capture_baseline()
    assert oracle.slo is None


def test_healthy_baseline_calibrates_latency_and_timeouts(oracle, monkeypatch):
    rounds = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=250, timeouts=180) for i in range(4)]
    monkeypatch.setattr(slo, "collect_rounds", lambda *a, **kw: rounds)
    oracle.capture_baseline()
    assert oracle.baseline_verdict.healthy
    assert oracle.slo.p99_limit_ms == 1250
    assert oracle.slo.timeout_limit == 0.16
    assert oracle.slo.error_limit == 0.05
