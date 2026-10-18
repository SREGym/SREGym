from types import SimpleNamespace

import pytest

from sregym.chaos import protected, slo
from sregym.conductor.oracles.application_recovery import RecoveryCheckResult
from sregym.conductor.oracles.slo_oracle import SLOMitigationOracle


@pytest.fixture
def oracle(monkeypatch):
    monkeypatch.setattr(protected, "snapshot", lambda _: {})
    app = SimpleNamespace(namespace="app", wrk=SimpleNamespace(wrk=SimpleNamespace(rate=100, duration=30)))
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


def test_observation_covers_recurrence_and_complete_rounds(oracle):
    recurring = SLOMitigationOracle(oracle.problem, sustained_seconds=180, recurrence_bound_s=390)
    assert recurring.sustained_seconds == 483
    assert recurring.evaluation_timeout_seconds >= recurring.buffer_seconds + 483 + 93 + 60


def test_temporary_repair_fails_after_recurrence(oracle, monkeypatch):
    recurring = SLOMitigationOracle(oracle.problem, buffer_seconds=0, recurrence_bound_s=390)
    recurring.slo = slo.SLO(target_rps=100)

    def collect(app, window_s, min_rounds):
        assert window_s >= 483
        return [
            slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10, non2xx=1500 if i >= 390 else 0)
            for i in range(0, int(window_s), 31)
        ]

    monkeypatch.setattr(slo, "collect_rounds", collect)
    assert recurring.evaluate()["success"] is False


@pytest.mark.parametrize("reason", [None, "application_data_changed", "application_response_invalid"])
def test_healthy_slo_requires_application_recovery(oracle, monkeypatch, reason):
    oracle.slo = slo.SLO(target_rps=100)
    rounds = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10) for i in range(4)]
    monkeypatch.setattr(slo, "collect_rounds", lambda *args, **kw: rounds)
    oracle.recovery_check = SimpleNamespace(evaluate=lambda: RecoveryCheckResult(reason))
    result = oracle.evaluate()
    assert result["success"] == (reason is None)
    if reason:
        assert result["reason"] == reason


def test_failed_application_baseline_does_not_leave_a_calibrated_oracle(oracle, monkeypatch):
    rounds = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10) for i in range(4)]
    monkeypatch.setattr(slo, "collect_rounds", lambda *args, **kw: rounds)

    def fail():
        raise ValueError("Unhealthy application")

    oracle.recovery_check = SimpleNamespace(capture_baseline=fail)
    with pytest.raises(ValueError, match="Unhealthy application"):
        oracle.capture_baseline()
    assert oracle.slo is None


def test_application_probe_transport_error_is_not_agent_failure(oracle, monkeypatch):
    import subprocess

    oracle.slo = slo.SLO(target_rps=100)
    rounds = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10) for i in range(4)]
    monkeypatch.setattr(slo, "collect_rounds", lambda *args, **kw: rounds)

    def unavailable():
        raise subprocess.CalledProcessError(1, ["kubectl", "exec"], stderr="workload probe connection lost")

    oracle.recovery_check = SimpleNamespace(evaluate=unavailable)
    result = oracle.evaluate()
    assert not result["success"]
    assert result["reason"] == "oracle_command_failed"
    assert result["failure_class"] == "ambiguous"
