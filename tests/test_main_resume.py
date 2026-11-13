import pytest

from sregym.results.resume import complete_resume_rows, resume_row_is_complete


def _row(attempt: int, **extra: str) -> dict[str, str]:
    return {"problem_id": "problem", "attempt": str(attempt), **extra}


def test_explicit_incomplete_and_failed_rows_do_not_satisfy_resume():
    rows = [
        _row(1, run_status="incomplete", **{"Diagnosis.success": "True"}),
        _row(2, deploy_failed="True"),
        _row(3, timed_out="True", **{"Diagnosis.success": "True", "Mitigation.success": "True"}),
    ]

    assert complete_resume_rows(rows, requested_attempts=3) == {}


def test_resume_keeps_only_complete_attempt_slots_and_deduplicates_them():
    first = _row(1, run_status="complete")
    replacement = _row(1, run_status="complete", marker="latest")
    outside_requested_range = _row(4, run_status="complete")

    rows = complete_resume_rows([first, replacement, outside_requested_range], requested_attempts=3)

    assert rows == {("problem", 1): replacement}


def test_legacy_diagnosis_only_row_is_rerun():
    diagnosis_only = _row(1, **{"Diagnosis.success": "True"})
    both_stages = _row(2, **{"Diagnosis.success": "False", "Mitigation.success": "False"})

    assert resume_row_is_complete(diagnosis_only) is False
    assert resume_row_is_complete(both_stages) is True


@pytest.mark.parametrize("run_status", ["", "complete", "incomplete"])
@pytest.mark.parametrize("reason", ["judge_parse_failure", "judge_backend_unavailable"])
def test_unscored_diagnosis_never_satisfies_resume(run_status, reason):
    row = _row(
        1,
        run_status=run_status,
        **{"Diagnosis.success": "", "Diagnosis.unscored_reason": reason, "Mitigation.success": "True"},
    )

    assert complete_resume_rows([row], requested_attempts=1) == {}


@pytest.mark.parametrize("stage", ["Diagnosis", "Mitigation"])
@pytest.mark.parametrize("success", ["True", "False"])
def test_resume_preserves_complete_single_stage_runs(stage, success):
    row = _row(1, run_status="complete", **{f"{stage}.success": success, "Diagnosis.unscored_reason": ""})

    assert resume_row_is_complete(row) is True


@pytest.mark.parametrize("evidence", ["reasoning", "oracles"])
def test_resume_retries_historical_unscored_evaluations_without_top_level_reason(evidence):
    row = _row(
        1,
        run_status="complete",
        **{"Diagnosis.success": "", f"Diagnosis.{evidence}": "recorded evaluation", "Mitigation.success": "True"},
    )

    assert resume_row_is_complete(row) is False
