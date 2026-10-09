import pytest

from sregym.results.report import build_report


def _row(attempt: int, diagnosis: object = True, mitigation: object = True, **extra):
    return {
        "problem_id": "example_problem",
        "attempt": str(attempt),
        "Diagnosis.success": str(diagnosis),
        "Mitigation.success": str(mitigation),
        **extra,
    }


def test_report_marks_five_complete_passes_as_saturated():
    report = build_report(
        [_row(attempt) for attempt in range(1, 6)],
        problem_id="example_problem",
        model="glm-4.7",
        requested_attempts=5,
    )

    assert "**Overall pass rate:** 5/5 (100%)" in report
    assert "🔴 **Saturated**" in report


@pytest.mark.parametrize(
    "classification", ["harness_error", "environment_error", "ambiguous", "unknown", " HARNESS_ERROR "]
)
@pytest.mark.parametrize("stage", ["Diagnosis", "Mitigation"])
def test_explicit_non_agent_failures_cannot_form_zero_of_five(classification, stage):
    rows = [
        _row(
            attempt, **{stage + ".success": "False", stage + ".failure_class": classification, "run_status": "complete"}
        )
        for attempt in range(1, 6)
    ]
    report = build_report(rows, problem_id="example_problem", model="test", requested_attempts=5)
    assert "**Complete attempts:** 0" in report
    assert "**Overall pass rate:** n/a" in report
    assert "**Inconclusive**" in report
    assert "**Unsolved**" not in report


def test_five_actual_agent_failures_remain_a_valid_difficulty_sample():
    rows = [_row(attempt, mitigation=False, **{"Mitigation.failure_class": " AGENT_ERROR "}) for attempt in range(1, 6)]
    report = build_report(rows, problem_id="example_problem", model="test", requested_attempts=5)
    assert "**Overall pass rate:** 0/5 (0%)" in report
    assert "**Unsolved**" in report


def test_invalid_duplicate_cannot_replace_valid_retry_in_report():
    valid = _row(1, **{"Diagnosis.failure_class": "harness_error"})
    invalid = _row(1, mitigation=False, **{"Mitigation.failure_class": "harness_error", "run_status": "complete"})
    for rows in ([invalid, valid], [valid, invalid]):
        report = build_report(rows, problem_id="example_problem", model="test", requested_attempts=1)
        assert "**Overall pass rate:** 1/1 (100%)" in report
        assert "**Saturated**" in report


def test_mixed_valid_and_invalid_attempts_remain_inconclusive():
    rows = [
        _row(1),
        _row(2, mitigation=False, **{"Mitigation.failure_class": "agent_error"}),
        _row(3, mitigation=False, **{"Mitigation.failure_class": "ambiguous"}),
    ]
    report = build_report(rows, problem_id="example_problem", model="test", requested_attempts=3)
    assert "**Overall pass rate:** 1/2 (50%)" in report
    assert "**Inconclusive**" in report


def test_report_keeps_mixed_result_as_not_saturated():
    report = build_report(
        [_row(1), _row(2, mitigation=False), _row(3)],
        problem_id="example_problem",
        model="glm-4.7",
        requested_attempts=3,
    )

    assert "**Overall pass rate:** 2/3 (67%)" in report
    assert "| 2 | ✅ | ❌ | ❌ | — |" in report
    assert "🟢 **Not saturated**" in report


def test_report_does_not_count_infrastructure_failure_as_model_failure():
    rows = [_row(1), {"problem_id": "example_problem", "attempt": "2", "deploy_failed": "True"}]

    report = build_report(
        rows,
        problem_id="example_problem",
        model="glm-4.7",
        requested_attempts=3,
    )

    assert "**Complete attempts:** 1" in report
    assert "**Overall pass rate:** 1/1 (100%)" in report
    assert "| 2 | — | — | ⚠️ | deployment failed |" in report
    assert "| 3 | — | — | ⚠️ | not run |" in report
    assert "⚠️ **Inconclusive**" in report


def test_report_explains_an_explicit_incomplete_attempt():
    rows = [
        {
            "problem_id": "example_problem",
            "attempt": "1",
            "Diagnosis.success": "True",
            "run_status": "incomplete",
            "incomplete_reason": "agent_exited_before_all_stages_completed",
            "incomplete_stage": "mitigation",
            "missing_stages": "mitigation",
        }
    ]

    report = build_report(
        rows,
        problem_id="example_problem",
        model="glm-4.7",
        requested_attempts=1,
    )

    assert "agent exited before all stages completed at mitigation" in report
    assert "⚠️ **Inconclusive**" in report


def test_report_includes_stage_for_an_explicit_timeout():
    rows = [
        {
            "problem_id": "example_problem",
            "attempt": "1",
            "run_status": "incomplete",
            "incomplete_reason": "agent_timeout",
            "incomplete_stage": "diagnosis",
            "missing_stages": "diagnosis,mitigation",
            "timed_out": "True",
        }
    ]

    report = build_report(
        rows,
        problem_id="example_problem",
        model="glm-4.7",
        requested_attempts=1,
    )

    assert "agent timeout at diagnosis" in report


def test_report_does_not_count_explicitly_incomplete_run_with_stage_results():
    row = _row(
        1,
        run_status="incomplete",
        incomplete_reason="agent_timeout",
        incomplete_stage="run",
    )

    report = build_report(
        [row],
        problem_id="example_problem",
        model="glm-4.7",
        requested_attempts=1,
    )

    assert "**Complete attempts:** 0" in report
    assert "**Diagnosis pass rate:** n/a" in report
    assert "**Mitigation pass rate:** n/a" in report
    assert "| 1 | ✅ | ✅ | ⚠️ | agent timeout at run |" in report
    assert "⚠️ **Inconclusive**" in report
