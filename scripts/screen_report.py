"""Summarise agent screens as solves out of attempts, with a per-attempt view.

Reports what an agent did rather than a derived rate: how many attempts it
solved, shown as one block per attempt in run order. Invalid attempts are marked
distinctly and excluded from the solve count, because an attempt that never
graded is not evidence about the agent.

    python scripts/screen_report.py [results_root ...]
"""

import argparse
import csv
import glob
import os
import statistics
from collections import defaultdict
from pathlib import Path

SOLVED, FAILED, INVALID = "█", "░", "×"
LEGEND = f"{SOLVED} solved   {FAILED} not solved   {INVALID} invalid (not the agent's result)"


def classify(row):
    """One attempt: solved, a genuine failure, or not valid evidence."""
    if (row.get("run_status") or "").lower() != "complete":
        return INVALID
    failure_class = (row.get("Mitigation.failure_class") or "").lower()
    if failure_class in ("environment_error", "ambiguous"):
        return INVALID
    return SOLVED if (row.get("Mitigation.success") or "").lower() == "true" else FAILED


#: Agent directory names the harness writes results under.
AGENTS = ("codex", "claudecode", "claude-code")


def collect(roots):
    """Group results by agent and problem, counting each attempt once.

    The harness writes a per-problem aggregate CSV *and* a copy inside each
    `run_N` directory. Counting both inflates every cohort, so the per-run
    copies are skipped and the problem id comes from the row rather than from a
    directory name, which varies by level.
    """
    cohorts = defaultdict(dict)
    for root in roots:
        for path in sorted(glob.glob(os.path.join(root, "**", "*_results.csv"), recursive=True)):
            name = Path(path).name
            parts = Path(path).parts
            # Each attempt is written three times: a per-problem CSV, a copy
            # inside its run_N directory, and an `<agent>_ALL_results.csv`
            # roll-up beside it. Count the per-problem CSV only.
            # `_voided/` holds screens withdrawn for a known reason -- a broken
            # fixture, or a task description that disclosed the answer. They are
            # kept for audit but must never appear in a result table.
            if (
                any(part.startswith("run_") for part in parts)
                or "_voided" in parts
                or name.endswith("_ALL_results.csv")
            ):
                continue
            agent = next((part for part in parts if part in AGENTS), None)
            if agent is None:
                agent = next((a for a in AGENTS if f"_{a}_results.csv" in name), "unknown")
            try:
                with open(path) as stream:
                    rows = list(csv.DictReader(stream))
            except OSError:
                continue
            # The run directory is the campaign: `results/<timestamp>/<agent>/...`.
            # Grouping without it merges separate screens of the same problem
            # into one inflated cohort, which is wrong and silently so -- a
            # voided screen's attempt would be averaged into its replacement's.
            campaign = next(
                (part for part in parts if len(part) == 9 and part[4] == "_" and part.replace("_", "").isdigit()),
                "unknown",
            )
            for row in rows:
                problem = row.get("problem_id") or Path(path).parent.name
                cohorts[(campaign, agent, problem)][row.get("attempt")] = row
    return {k: list(v.values()) for k, v in cohorts.items()}


def report(cohorts):
    if not cohorts:
        print("No results found.")
        return
    width = max(len(p) for _, _, p in cohorts) + 2
    print(f"{'problem':<{width}} {'agent':<11} {'screen':<10} attempts  solved")
    print("-" * (width + 46))
    for (campaign, agent, problem), rows in sorted(cohorts.items(), key=lambda kv: (kv[0][2], kv[0][0])):
        marks = [classify(r) for r in rows]
        valid = [m for m in marks if m != INVALID]
        solved = marks.count(SOLVED)
        times = [float(r["TTM"]) for r in rows if r.get("TTM") and classify(r) != INVALID]
        detail = f"{solved}/{len(valid)} valid"
        if len(valid) != len(marks):
            detail += f"  ({marks.count(INVALID)} invalid)"
        if times:
            detail += f"  median {statistics.median(times):.0f}s"
        print(f"{problem:<{width}} {agent:<11} {campaign:<10} {''.join(marks):<9} {detail}")
    print()
    print(LEGEND)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", default=["results"], help="directories to scan")
    args = parser.parse_args()
    report(collect(args.roots))
