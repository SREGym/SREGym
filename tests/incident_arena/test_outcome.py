from types import SimpleNamespace

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.incident_arena import (
    CheckResult,
    IncidentArenaMitigationOracle,
    OutcomeSpec,
    evaluate_outcome,
    relaxed_ceiling,
    relaxed_floor,
)


def _summary(**overrides):
    summary = {
        "offered": 100,
        "dropped": 0,
        "failures": 5,
        "good": 90,
        "error_rate": 0.05,
        "goodput_ratio": 0.9,
        "latency": {"peak": {"n": 50, "p_ms": 400.0}, "trough": {"n": 50, "p_ms": 300.0}},
        "by_driver": {
            "write_readback": {"offered": 100, "error_rate": 0.05, "goodput_ratio": 0.9, "latency": {}},
        },
    }
    summary.update(overrides)
    return summary


THRESHOLDS = {
    "p99_ms_by_phase": {"peak": 462, "trough": 467},
    "error_rate_max": 0.05,
    "goodput_min_ratio": 0.9,
    "by_driver": {"write_readback": {"goodput_min_ratio": 0.75, "error_rate_max": 0.1}},
    "latency_percentile": 90,
}


def _by_name(results):
    return {r.name: r for r in results}


def test_bands_use_the_incident_arena_tolerance():
    assert relaxed_ceiling(0.1) == 0.12
    assert relaxed_ceiling(0.9, maximum=1.0) == 1.0
    assert relaxed_floor(0.8) == 0.8 * 0.8


def test_healthy_soak_passes_every_gate():
    results = evaluate_outcome(_summary(), OutcomeSpec(THRESHOLDS))
    assert all(r.passed for r in results), [r for r in results if not r.passed]
    assert {"traffic_observed", "sustained_error_rate", "sustained_correct_goodput", "sustained_latency"} <= set(
        _by_name(results)
    )


def test_error_rate_beyond_relaxed_ceiling_fails():
    results = _by_name(evaluate_outcome(_summary(error_rate=0.07), OutcomeSpec(THRESHOLDS)))
    assert not results["sustained_error_rate"].passed
    assert results["sustained_error_rate"].reason == "service_unhealthy"


def test_latency_is_only_graded_when_the_task_gates_it():
    slow = _summary(latency={"peak": {"n": 5, "p_ms": 5000.0}, "trough": {"n": 5, "p_ms": 1.0}})
    assert not _by_name(evaluate_outcome(slow, OutcomeSpec(THRESHOLDS)))["sustained_latency"].passed
    assert "sustained_latency" not in _by_name(evaluate_outcome(slow, OutcomeSpec(THRESHOLDS, gate_latency=False)))


def test_missing_driver_traffic_fails_its_lane():
    summary = _summary(by_driver={})
    assert not _by_name(evaluate_outcome(summary, OutcomeSpec(THRESHOLDS)))[
        "driver_write_readback_within_limits"
    ].passed


def test_no_traffic_short_circuits():
    results = evaluate_outcome(_summary(offered=0), OutcomeSpec(THRESHOLDS))
    assert [r.name for r in results] == ["traffic_observed"]
    assert results[0].reason == "no_traffic_observed"


class _Workload:
    def __init__(self):
        self.calls = []

    def latest_sent_s(self):
        self.calls.append("latest")
        return 100.0

    def summary(self, since, latency_percentile, settle_s):
        self.calls.append(("summary", since, latency_percentile, settle_s))
        return _summary()


class _Problem:
    def __init__(self, declaration_pass=True, challenge_pass=True):
        self.app = SimpleNamespace(wrk=_Workload())
        self.phases = []
        self.declaration_pass = declaration_pass
        self.challenge_pass = challenge_pass
        self.baseline_captured = False

    def capture_baseline(self):
        self.baseline_captured = True

    def outcome_spec(self):
        return OutcomeSpec(THRESHOLDS, soak_s=0)

    def run_checks(self, phase):
        self.phases.append(phase)
        passed = self.declaration_pass or phase != "declaration"
        return [CheckResult(f"root_cause@{phase}", passed)]

    def challenges(self):
        return [lambda: CheckResult("restart", self.challenge_pass, reason="repair_not_durable")]


def test_oracle_passes_after_declaration_challenge_soak_and_outcome():
    problem = _Problem()
    oracle = IncidentArenaMitigationOracle(problem)
    oracle.capture_baseline()
    verdict = oracle.evaluate()
    assert problem.baseline_captured
    assert verdict["success"] is True
    assert problem.phases == ["declaration", "soak_end"]
    assert problem.app.wrk.calls[-1] == ("summary", 100.0, 90.0, 0.0)


def test_oracle_fails_fast_when_the_fault_is_still_present():
    problem = _Problem(declaration_pass=False)
    verdict = IncidentArenaMitigationOracle(problem).evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "fault_still_present"
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR
    assert problem.phases == ["declaration"]
    assert problem.app.wrk.calls == []


def test_oracle_reports_non_durable_repairs():
    verdict = IncidentArenaMitigationOracle(_Problem(challenge_pass=False)).evaluate()
    assert verdict["reason"] == "repair_not_durable"
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR
