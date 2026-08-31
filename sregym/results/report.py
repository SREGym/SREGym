"""Build a Markdown difficulty report from one SREGym results CSV."""

import argparse
import csv
import os
from pathlib import Path


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _rate(passes: int, evaluated: int) -> str:
    if evaluated == 0:
        return "n/a"
    return f"{passes}/{evaluated} ({passes / evaluated:.0%})"


def build_report(
    rows: list[dict[str, str]],
    *,
    problem_id: str,
    model: str,
    requested_attempts: int,
) -> str:
    """Return a report whose saturation decision requires every requested run."""
    selected = [row for row in rows if row.get("problem_id") == problem_id]
    attempts: dict[int, dict[str, str]] = {}
    for fallback_attempt, row in enumerate(selected, start=1):
        try:
            attempt = int(row.get("attempt", ""))
        except (TypeError, ValueError):
            attempt = fallback_attempt
        attempts[attempt] = row

    diagnosis_evaluated = 0
    diagnosis_passes = 0
    mitigation_evaluated = 0
    mitigation_passes = 0
    complete_attempts = 0
    overall_passes = 0
    table_rows: list[str] = []

    for attempt in range(1, requested_attempts + 1):
        row = attempts.get(attempt)
        if row is None:
            table_rows.append(f"| {attempt} | — | — | ⚠️ | not run |")
            continue

        diagnosis = _as_bool(row.get("Diagnosis.success"))
        mitigation = _as_bool(row.get("Mitigation.success"))
        attempt_incomplete = row.get("run_status") == "incomplete" or diagnosis is None or mitigation is None
        if not attempt_incomplete and diagnosis is not None:
            diagnosis_evaluated += 1
            diagnosis_passes += int(diagnosis)
        if not attempt_incomplete and mitigation is not None:
            mitigation_evaluated += 1
            mitigation_passes += int(mitigation)

        detail = ""
        if _as_bool(row.get("deploy_failed")):
            detail = "deployment failed"
        elif row.get("run_status") == "incomplete":
            reason = row.get("incomplete_reason") or "missing stage results"
            stage = row.get("incomplete_stage")
            detail = reason.replace("_", " ")
            if stage:
                detail = f"{detail} at {stage}"
        elif _as_bool(row.get("timed_out")):
            detail = "agent timed out"
        elif diagnosis is None or mitigation is None:
            missing = []
            if diagnosis is None:
                missing.append("diagnosis")
            if mitigation is None:
                missing.append("mitigation")
            detail = f"missing {' and '.join(missing)} result"

        diagnosis_text = "✅" if diagnosis is True else "❌" if diagnosis is False else "—"
        mitigation_text = "✅" if mitigation is True else "❌" if mitigation is False else "—"
        if attempt_incomplete:
            overall_text = "⚠️"
        else:
            complete_attempts += 1
            overall = diagnosis and mitigation
            overall_passes += int(overall)
            overall_text = "✅" if overall else "❌"
        table_rows.append(f"| {attempt} | {diagnosis_text} | {mitigation_text} | {overall_text} | {detail or '—'} |")

    if complete_attempts != requested_attempts:
        decision = "⚠️ **Inconclusive** — one or more requested attempts did not produce both stage results; rerun it."
    elif overall_passes == requested_attempts:
        decision = "🔴 **Saturated** — every requested attempt passed; do not keep it as a difficulty candidate."
    elif overall_passes == 0:
        decision = (
            "🟠 **Unsolved** — no complete attempt passed; review whether the problem is too difficult or broken."
        )
    else:
        decision = "🟢 **Not saturated** — at least one attempt passed and at least one failed; keep it as a candidate."

    lines = [
        "# Z.ai Problem Difficulty Evaluation",
        "",
        f"- **Problem:** `{problem_id}`",
        f"- **Model:** `{model}` via the Codex harness",
        f"- **Requested attempts:** {requested_attempts}",
        f"- **Complete attempts:** {complete_attempts}",
        f"- **Diagnosis pass rate:** {_rate(diagnosis_passes, diagnosis_evaluated)}",
        f"- **Mitigation pass rate:** {_rate(mitigation_passes, mitigation_evaluated)}",
        f"- **Overall pass rate:** {_rate(overall_passes, complete_attempts)}",
        "",
        "Overall requires both diagnosis and mitigation to pass. Infrastructure-incomplete attempts are not counted as model failures.",
        "",
        "| Attempt | Diagnosis | Mitigation | Overall | Notes |",
        "| ---: | :---: | :---: | :---: | --- |",
        *table_rows,
        "",
        "## Screening decision",
        "",
        decision,
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="SREGym results CSV")
    parser.add_argument("--problem", required=True, help="Problem ID to report")
    parser.add_argument("--model", required=True, help="Z.ai model used by Codex")
    parser.add_argument("--attempts", required=True, type=int, help="Number of requested attempts")
    parser.add_argument("--output", required=True, type=Path, help="Markdown report path")
    parser.add_argument(
        "--github-summary",
        type=Path,
        default=Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None,
        help="Optional GitHub step-summary file to append",
    )
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be positive")

    with args.csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    report = build_report(
        rows,
        problem_id=args.problem,
        model=args.model,
        requested_attempts=args.attempts,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    if args.github_summary is not None:
        with args.github_summary.open("a", encoding="utf-8") as handle:
            handle.write(report)
    print(report)


if __name__ == "__main__":
    main()
