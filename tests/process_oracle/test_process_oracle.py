import json
import tempfile
from pathlib import Path

import pytest

from sregym.conductor.oracles.process_oracle import ProcessOracle

FIXTURES = Path(__file__).parent / "fixtures"
SPEC = Path(__file__).parent.parent.parent / "sregym/conductor/oracles/llm_as_a_judge/process_specs/cfs_cpu_throttling_hotel_reservation.yaml"


@pytest.fixture
def oracle():
    return ProcessOracle(spec_path=SPEC)


def test_good_agent_cgroup_path_passes(oracle):
    result = oracle.evaluate(FIXTURES / "good_agent.json")
    assert result.passed, f"Expected PASS\n{result.report}"
    assert result.symptom_reached
    assert result.root_cause_reached


def test_good_agent_prometheus_path_passes(oracle):
    result = oracle.evaluate(FIXTURES / "good_agent_prometheus_path.json")
    assert result.passed, f"Expected PASS\n{result.report}"
    assert result.symptom_reached
    assert result.root_cause_reached


def test_bad_agent_fails(oracle):
    result = oracle.evaluate(FIXTURES / "bad_agent.json")
    assert not result.passed, f"Expected FAIL\n{result.report}"
    assert not result.root_cause_reached


def test_bad_agent_misses_root_cause_node(oracle):
    result = oracle.evaluate(FIXTURES / "bad_agent.json")
    missed = [r for r in result.chain_results if not r.touched]
    assert len(missed) > 0


def test_good_agent_score(oracle):
    result = oracle.evaluate(FIXTURES / "good_agent.json")
    assert result.score == 1.0


def test_bad_agent_score(oracle):
    result = oracle.evaluate(FIXTURES / "bad_agent.json")
    good = oracle.evaluate(FIXTURES / "good_agent.json")
    assert result.score < good.score


def test_for_problem_loader():
    oracle = ProcessOracle.for_problem("cfs_cpu_throttling_hotel_reservation")
    result = oracle.evaluate(FIXTURES / "good_agent.json")
    assert result.passed


def test_tool_call_count(oracle):
    good = oracle.evaluate(FIXTURES / "good_agent.json")
    bad = oracle.evaluate(FIXTURES / "bad_agent.json")
    assert good.tool_call_count > bad.tool_call_count


def test_report_contains_node_id(oracle):
    result = oracle.evaluate(FIXTURES / "good_agent.json")
    assert "intermediate_throttling_evidence" in result.report
    assert "TOUCH" in result.report


def test_unique_command_count_good_agent(oracle):
    result = oracle.evaluate(FIXTURES / "good_agent.json")
    assert result.unique_command_count > 0
    assert result.unique_command_count <= result.tool_call_count


def test_unique_command_count_less_than_total_when_repeated(oracle):
    bad = oracle.evaluate(FIXTURES / "bad_agent.json")
    assert bad.unique_command_count <= bad.tool_call_count


def test_anchoring_risk_detected(oracle):
    result = oracle.evaluate(FIXTURES / "anchoring_agent.json")
    assert result.rf13_anchoring_risk, f"Expected anchoring risk\n{result.report}"


def test_anchoring_risk_not_flagged_for_good_agent(oracle):
    result = oracle.evaluate(FIXTURES / "good_agent.json")
    assert not result.rf13_anchoring_risk, f"Good agent should not be flagged\n{result.report}"


def test_anchoring_agent_fails(oracle):
    result = oracle.evaluate(FIXTURES / "anchoring_agent.json")
    assert not result.passed, f"Anchoring agent should fail\n{result.report}"


def test_anchoring_risk_in_report(oracle):
    result = oracle.evaluate(FIXTURES / "anchoring_agent.json")
    assert "anchoring" in result.report.lower()


def test_invalid_format_raises(oracle):
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump({"schema_version": "internal-v1", "steps": []}, f)
        tmp = Path(f.name)
    with pytest.raises(ValueError, match="ATIF"):
        oracle.evaluate(tmp)
    tmp.unlink()


# ---------------------------------------------------------------------------
# Baseline gate: signals that also fire on a healthy run are non-discriminative
# ---------------------------------------------------------------------------


@pytest.fixture
def oracle_with_baseline():
    return ProcessOracle(
        spec_path=SPEC,
        baseline_paths=[FIXTURES / "healthy_baseline.json"],
    )


def test_baseline_run_count_recorded(oracle_with_baseline):
    result = oracle_with_baseline.evaluate(FIXTURES / "good_agent.json")
    assert result.baseline_run_count == 1


def test_no_baseline_leaves_scoring_unchanged(oracle, oracle_with_baseline):
    no_baseline = oracle.evaluate(FIXTURES / "good_agent.json")
    leaked = [
        p
        for r in no_baseline.chain_results
        for (p, _, _) in r.signals_leaked
    ]
    assert leaked == []


def test_loose_symptom_signal_leaks_on_baseline(oracle_with_baseline):
    result = oracle_with_baseline.evaluate(FIXTURES / "good_agent.json")
    symptom = result.chain_results[0]
    leaked_patterns = [p for (p, _, _) in symptom.signals_leaked]
    assert "geo" in leaked_patterns, result.report


def test_leaked_symptom_is_not_credited(oracle_with_baseline):
    result = oracle_with_baseline.evaluate(FIXTURES / "good_agent.json")
    assert not result.symptom_reached, result.report


def test_discriminative_signal_survives_baseline(oracle_with_baseline):
    result = oracle_with_baseline.evaluate(FIXTURES / "good_agent.json")
    intermediate = result.chain_results[1]
    assert intermediate.signals_leaked == []
    assert intermediate.touched, result.report


def test_baseline_leak_reported(oracle_with_baseline):
    result = oracle_with_baseline.evaluate(FIXTURES / "good_agent.json")
    assert "baseline-leak" in result.report
    assert "Baseline gate" in result.report


def test_unverifiable_symptom_node_tracked(oracle_with_baseline):
    result = oracle_with_baseline.evaluate(FIXTURES / "good_agent.json")
    assert "symptom" in result.unverifiable_nodes


def test_rf05_suppressed_for_unverifiable_symptom(oracle_with_baseline):
    result = oracle_with_baseline.evaluate(FIXTURES / "good_agent.json")
    assert not result.rf05_spurious_attribution, result.report


def test_spec_quality_warning_in_report(oracle_with_baseline):
    result = oracle_with_baseline.evaluate(FIXTURES / "good_agent.json")
    assert "Spec quality" in result.report


def test_rf05_still_fires_for_genuine_spurious_attribution(oracle):
    result = oracle.evaluate(FIXTURES / "root_cause_only.json")
    assert result.root_cause_reached
    assert not result.symptom_reached
    assert result.rf05_spurious_attribution
    assert result.unverifiable_nodes == []
