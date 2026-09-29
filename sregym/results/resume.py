def resume_row_is_complete(row: dict[str, str]) -> bool:
    """Return whether a prior CSV row satisfies one requested attempt."""
    run_status = str(row.get("run_status", "")).strip().lower()
    if run_status:
        return run_status == "complete"
    if str(row.get("deploy_failed", "")).strip().lower() in {"true", "1", "yes"}:
        return False
    if str(row.get("timed_out", "")).strip().lower() in {"true", "1", "yes"}:
        return False
    # Legacy rows have no run_status. Require both standard stage results so
    # diagnosis-only rows from the old lost-submission bug are rerun rather
    # than counted as complete.
    return bool(str(row.get("Diagnosis.success", "")).strip()) and bool(str(row.get("Mitigation.success", "")).strip())


def complete_resume_rows(
    rows: list[dict[str, str]],
    requested_attempts: int,
) -> dict[tuple[str, int], dict[str, str]]:
    """Keep one complete row for each valid problem/attempt slot."""
    complete_rows: dict[tuple[str, int], dict[str, str]] = {}
    for row in rows:
        try:
            attempt_number = int(row.get("attempt", ""))
        except (TypeError, ValueError):
            continue
        if not 1 <= attempt_number <= requested_attempts or not resume_row_is_complete(row):
            continue
        key = (row.get("problem_id", ""), attempt_number)
        if key[0]:
            complete_rows[key] = row
    return complete_rows
