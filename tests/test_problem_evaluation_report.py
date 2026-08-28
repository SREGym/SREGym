from sregym.problem_evaluation_report import build_report


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


def test_report_keeps_mixed_result_as_not_saturated():
    report = build_report(
        [_row(1), _row(2, mitigation=False), _row(3)],
        problem_id="example_problem",
        model="glm-4.7",
        requested_attempts=3,
    )

    assert "**Overall pass rate:** 2/3 (67%)" in report
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
    assert "| 2 | — | — | Incomplete | deployment failed |" in report
    assert "| 3 | — | — | Incomplete | not run |" in report
    assert "⚠️ **Inconclusive**" in report
