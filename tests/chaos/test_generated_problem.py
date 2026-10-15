from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.chaos import protected, slo, telemetry
from sregym.conductor.problems.generated_problem import GeneratedProblem


@pytest.fixture
def problem(monkeypatch):
    healthy = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10) for i in range(5)]
    broken = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10, non2xx=1500) for i in range(5)]
    policy = slo.SLO(target_rps=100)
    oracle = SimpleNamespace(
        slo=policy, baseline_verdict=slo.judge(policy, healthy), measure=Mock(return_value=slo.judge(policy, broken))
    )
    p = GeneratedProblem.__new__(GeneratedProblem)
    p.problem_id = "generated-example"
    p.namespace = "app"
    p.fault_injected = False
    p.mitigation_oracle = oracle
    p.alerts = SimpleNamespace(capture_baseline=lambda: None, new=lambda: [])
    p.workspace = None
    p.artifact = SimpleNamespace(
        workspace=Mock(
            return_value=SimpleNamespace(run=Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr="")))
        )
    )
    p._write_report = Mock()
    monkeypatch.setattr(protected, "snapshot", lambda _: {"Job/app/workload": "original"})
    monkeypatch.setattr(protected, "leak_scan", lambda _: {})
    monkeypatch.setattr(protected, "workload_running", lambda _: True)
    monkeypatch.setattr(telemetry.Signals, "capture_baseline", lambda _: None)
    monkeypatch.setattr(telemetry.Signals, "changes", lambda _: {"log_surges": {}, "cpu_surges": {}})
    monkeypatch.setenv("CHAOS_PERSISTENCE_S", "0")
    return p


def test_real_request_failures_are_observable_without_new_alerts(problem):
    problem.inject_fault()
    assert problem.fault_injected
    assert problem.gates["impact_pass"]
    assert problem.gates["observability_pass"]


def test_no_effect_injection_is_setup_failure(problem):
    problem.mitigation_oracle.measure.return_value = problem.mitigation_oracle.baseline_verdict
    with pytest.raises(RuntimeError, match="impact_pass"):
        problem.inject_fault()
    assert not problem.fault_injected
    problem._write_report.assert_called_once()
    assert problem.workspace is not None  # Partial injection still needs recovery.


def test_missing_measurement_cannot_establish_impact(problem):
    rounds = [slo.RoundStats(time=i, requests=3000, duration_s=30, non2xx=1500) for i in range(5)]
    problem.mitigation_oracle.measure.return_value = slo.judge(problem.mitigation_oracle.slo, rounds)
    with pytest.raises(RuntimeError, match="complete_workload_measurement"):
        problem.inject_fault()


def test_stopped_workload_is_setup_failure(problem, monkeypatch):
    monkeypatch.setattr(protected, "workload_running", lambda _: False)
    with pytest.raises(RuntimeError, match="integrity_pass"):
        problem.inject_fault()


def test_modified_measurement_resources_are_setup_failure(problem, monkeypatch):
    snapshots = iter([{"Job/app/workload": "original"}, {"Job/app/workload": "changed"}])
    monkeypatch.setattr(protected, "snapshot", lambda _: next(snapshots))
    with pytest.raises(RuntimeError, match="integrity_pass"):
        problem.inject_fault()


def test_protected_resource_api_failure_is_not_an_empty_snapshot(monkeypatch):
    import subprocess

    monkeypatch.setattr(
        protected.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=1, stdout="", stderr="API unavailable"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        protected.snapshot("app")
