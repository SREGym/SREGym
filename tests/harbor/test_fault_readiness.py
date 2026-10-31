"""Readiness shortcuts require positive evidence and never shortcut final grading."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.slo_oracle import SLOMitigationOracle
from sregym.harbor.backend import ConductorSession, slo_fault_evidence


class Probe(Oracle):
    def __init__(self, verdict):
        super().__init__(None)
        self.verdict = verdict
        self.calls = 0

    def evaluate(self):
        self.calls += 1
        return dict(self.verdict)


def test_definitive_fault_skips_traffic_only_for_readiness():
    fault = Probe({"success": False, "reason": "fault_still_present", "failure_class": "agent_error"})
    health = Probe({"success": True})
    oracle = CompoundedOracle(None, fault=fault, app_health=health)
    readiness = oracle.evaluate_fault_readiness()
    assert readiness["failure_class"] == "agent_error"
    assert [child["name"] for child in readiness["oracles"]] == ["fault"]
    assert (fault.calls, health.calls) == (1, 0)
    final = oracle.evaluate()
    assert [child["name"] for child in final["oracles"]] == ["fault", "app_health"]
    assert (fault.calls, health.calls) == (2, 1)


@pytest.mark.parametrize("failure_class", ["ambiguous", "environment_error", "harness_error", None])
def test_inconclusive_fault_does_not_skip_health_or_repeat_fault(failure_class):
    fault = Probe({"success": False, "reason": "cannot_tell", "failure_class": failure_class})
    health = Probe({"success": True})
    oracle = CompoundedOracle(None, fault=fault, app_health=health)
    oracle.evaluate_fault_readiness()
    assert (fault.calls, health.calls) == (1, 1)


def test_healthy_fault_check_still_requires_application_health():
    fault = Probe({"success": True})
    health = Probe({"success": False, "reason": "loadgen_unavailable", "failure_class": "ambiguous"})
    result = CompoundedOracle(None, fault=fault, app_health=health).evaluate_fault_readiness()
    assert result["success"] is False
    assert result["failure_class"] == "ambiguous"
    assert (fault.calls, health.calls) == (1, 1)


def test_other_compositions_keep_all_their_checks():
    fault = Probe({"success": False, "reason": "fault_still_present", "failure_class": "agent_error"})
    health = Probe({"success": True})
    CompoundedOracle(None, fault, health).evaluate_fault_readiness()
    assert (fault.calls, health.calls) == (1, 1)


def gates():
    return {
        "problem_id": "example",
        "impact_pass": True,
        "observability_pass": True,
        "integrity_pass": True,
        "no_leak_pass": True,
        "request_symptoms": ["error_ratio=1.000>0.050"],
        "baseline": {"healthy": True, "measurement_complete": True, "rounds": 3},
        "impact": {
            "violated": True,
            "measurement_complete": True,
            "rounds": 4,
            "details": [{"time": 900}, {"time": 931}, {"time": 962}, {"time": 993}],
        },
    }


def test_slo_readiness_uses_recent_complete_positive_injection_evidence():
    value = gates()
    before = deepcopy(value)
    result = slo_fault_evidence(value, "example", 4, now=1000)
    assert result["success"] is False and result["failure_class"] == "agent_error"
    assert result["detail"]["readiness_evidence"] == "validated_injection_gates"
    assert value == before


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("problem_id",), "another_trial"),
        (("impact_pass",), False),
        (("observability_pass",), False),
        (("integrity_pass",), False),
        (("no_leak_pass",), False),
        (("request_symptoms",), []),
        (("baseline", "healthy"), False),
        (("baseline", "measurement_complete"), False),
        (("baseline", "rounds"), 2),
        (("impact", "violated"), False),
        (("impact", "measurement_complete"), False),
        (("impact", "rounds"), 3),
        (("impact", "details"), []),
        (("impact", "details"), [{"time": 800}]),
        (("impact", "details"), [{"time": 1100}]),
        (("impact", "details"), [{"time": float("nan")}]),
    ],
)
def test_slo_shortcut_rejects_incomplete_stale_or_unproven_evidence(path, replacement):
    value = gates()
    item = value
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = replacement
    assert slo_fault_evidence(value, "example", 4, now=1000) is None


def test_slo_shortcut_does_not_apply_without_injection_gates():
    assert slo_fault_evidence(None, "example", 4, now=1000) is None


def measured_error_gates():
    value = gates()
    value["impact"]["measurement_complete"] = False
    value["impact"]["details"] = [
        {
            "time": timestamp,
            "requests": requests,
            "rps": 98.9,
            "error_ratio": ratio,
            "timeout_ratio": 0.063,
            "p99_ms": 3.5,
            "violations": [f"error_ratio={ratio}>0.050"],
        }
        for timestamp, requests, ratio in [
            (800, 2973, 0.6485),
            (845, 2971, 0.6395),
            (890, 2972, 0.5855),
            (935, 2972, 0.7463),
        ]
    ]
    return value


def test_missing_coverage_does_not_erase_positive_request_failure_evidence():
    result = slo_fault_evidence(measured_error_gates(), "example", 4, now=1000)
    assert result["reason"] == "fault_still_present"
    assert result["detail"]["readiness_evidence"] == "validated_request_failures"
    assert result["detail"]["window"]["measurement_complete"] is False


@pytest.mark.parametrize("field,value", [("requests", 0), ("rps", 0), ("p99_ms", None), ("error_ratio", 0)])
def test_missing_or_healthy_samples_cannot_be_used_as_request_failure_evidence(field, value):
    evidence = measured_error_gates()
    evidence["impact"]["details"][-1][field] = value
    assert slo_fault_evidence(evidence, "example", 4, now=1000) is None


def test_repeated_cached_sample_is_not_four_independent_request_rounds():
    evidence = measured_error_gates()
    for detail in evidence["impact"]["details"]:
        detail["time"] = 935
    assert slo_fault_evidence(evidence, "example", 4, now=1000) is None


def test_incomplete_low_throughput_without_measured_errors_is_not_proof():
    evidence = measured_error_gates()
    for detail in evidence["impact"]["details"]:
        detail.update(error_ratio=0, timeout_ratio=0, violations=["rps=5.0<70.0"])
    assert slo_fault_evidence(evidence, "example", 4, now=1000) is None


def session(problem, grade):
    value = ConductorSession("example", advertise_host="127.0.0.1", proxy_port=16443)
    value.conductor = SimpleNamespace(problem=problem, _evaluate_mitigation=grade)
    return value


def test_conductor_session_uses_slo_proof_without_changing_the_grade(monkeypatch):
    oracle = SLOMitigationOracle.__new__(SLOMitigationOracle)
    oracle.min_rounds = 4
    problem = SimpleNamespace(mitigation_oracle=oracle, gates=gates())
    calls = []
    value = session(problem, lambda _: calls.append("full-grade") or {"success": True})
    monkeypatch.setattr("sregym.harbor.backend.time.time", lambda: 1000)
    assert value.check_fault()["failure_class"] == "agent_error"
    assert calls == []
    assert value.grade()["success"] is True
    assert calls == ["full-grade"]


def test_conductor_session_falls_back_to_full_measurement_for_incomplete_gates(monkeypatch):
    oracle = SLOMitigationOracle.__new__(SLOMitigationOracle)
    oracle.min_rounds = 4
    evidence = gates()
    evidence["impact"]["measurement_complete"] = False
    value = session(
        SimpleNamespace(mitigation_oracle=oracle, gates=evidence),
        lambda _: {"success": False, "reason": "slo_incomplete_measurement", "failure_class": "ambiguous"},
    )
    monkeypatch.setattr("sregym.harbor.backend.time.time", lambda: 1000)
    assert value.check_fault()["failure_class"] == "ambiguous"


def test_conductor_session_uses_compound_readiness_but_grades_every_child():
    fault = Probe({"success": False, "reason": "fault_still_present", "failure_class": "agent_error"})
    health = Probe({"success": True})
    oracle = CompoundedOracle(None, fault=fault, app_health=health)
    value = session(SimpleNamespace(mitigation_oracle=oracle), lambda _: oracle.evaluate())
    assert value.check_fault()["failure_class"] == "agent_error"
    assert (fault.calls, health.calls) == (1, 0)
    value.grade()
    assert (fault.calls, health.calls) == (2, 1)
