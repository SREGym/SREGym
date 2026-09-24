"""Re-judge real agent submissions from ``stratus_results.csv`` against the
reference answers that issue #747 updated with mechanism detail.

The CSV at ``tests/llm_as_a_judge/stratus_results.csv`` is a snapshot of a
historical evaluation run: for each (experiment, problem_id, attempt) it
records the agent's diagnosis text and the judge's verdict at the time. This
test takes the subset of rows whose ``problem_id`` corresponds to a Problem
whose reference answer was updated in the issue #747 fix, runs the LLM judge
against the *new* reference answer (with mechanism detail) and the same
agent submission, and writes a side-by-side CSV of old vs new scores under
``tests/llm_as_a_judge/fixtures/stratus_rejudged.csv``.

The expected outcome is that rows whose submissions correctly named the
flagd mechanism but were unfairly penalized (issue #747) recover composite
score; rows that named a genuinely wrong fault stay low.

Skipped if ``JUDGE_MODEL_ID`` is not set (live judge required).
"""

from __future__ import annotations

import csv
import importlib
import os
from pathlib import Path
from statistics import mean

import pytest

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("JUDGE_MODEL_ID"),
        reason="JUDGE_MODEL_ID is not set; live judge is unavailable",
    ),
    pytest.mark.slow,
]

CSV_PATH = Path(__file__).parent / "stratus_results.csv"
FIXTURES_DIR = Path(__file__).parent / "fixtures"

# CSV ``problem_id`` -> dotted Problem class to reconstruct for re-judging.
# Keys match the registry IDs from sregym/conductor/problems/registry.py.
# Only includes problems whose root_cause was updated to add mechanism detail
# in the issue #747 fix, plus ``ad_service_high_cpu`` as an unchanged control.
EDITED_PROBLEMS = {
    "astronomy_shop_ad_service_failure": "sregym.conductor.problems.ad_service_failure.AdServiceFailure",
    "astronomy_shop_ad_service_manual_gc": "sregym.conductor.problems.ad_service_manual_gc.AdServiceManualGc",
    "astronomy_shop_ad_service_image_slow_load": "sregym.conductor.problems.image_slow_load.ImageSlowLoad",
    "astronomy_shop_cart_service_failure": "sregym.conductor.problems.cart_service_failure.CartServiceFailure",
    "astronomy_shop_failed_readiness_probe": "sregym.conductor.problems.failed_readiness_probe.FailedReadinessProbe",
    "astronomy_shop_payment_service_failure": "sregym.conductor.problems.payment_service_failure.PaymentServiceFailure",
    "astronomy_shop_payment_service_unreachable": "sregym.conductor.problems.payment_service_unreachable.PaymentServiceUnreachable",
    "astronomy_shop_product_catalog_service_failure": "sregym.conductor.problems.product_catalog_failure.ProductCatalogServiceFailure",
    "kafka_queue_problems": "sregym.conductor.problems.kafka_queue_problems.KafkaQueueProblems",
    "loadgenerator_flood_homepage": "sregym.conductor.problems.loadgenerator_flood_homepage.LoadGeneratorFloodHomepage",
}

CONTROL_PROBLEMS = {
    "astronomy_shop_ad_service_high_cpu": "sregym.conductor.problems.ad_service_high_cpu.AdServiceHighCpu",
}

ALL_TARGET_PROBLEMS = {**EDITED_PROBLEMS, **CONTROL_PROBLEMS}


def _construct_problem(dotted_class: str):
    module, cls = dotted_class.rsplit(".", 1)
    return getattr(importlib.import_module(module), cls)()


def _load_impacted_rows() -> list[dict]:
    with CSV_PATH.open() as f:
        return [r for r in csv.DictReader(f) if r["problem_id"] in ALL_TARGET_PROBLEMS]


def _safe_float(s: str) -> float | None:
    try:
        return float(s) if s else None
    except ValueError:
        return None


def test_rejudge_impacted_stratus_rows():
    """Re-judge every impacted stratus_results row against the new root_cause."""
    from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import (
        LLMAsAJudgeOracle,
    )

    FIXTURES_DIR.mkdir(exist_ok=True)
    rows = _load_impacted_rows()
    assert rows, f"No impacted rows found in {CSV_PATH}"

    # Cache constructed problems so we don't re-import for every attempt.
    problem_cache: dict[str, object] = {}
    for pid, dotted in ALL_TARGET_PROBLEMS.items():
        problem_cache[pid] = _construct_problem(dotted)

    output_rows: list[dict] = []
    deltas: list[float] = []
    edited_deltas: list[float] = []
    errors: list[str] = []

    for row in rows:
        pid = row["problem_id"]
        problem = problem_cache[pid]
        submission = row.get("Diagnosis.submission") or ""

        if not submission.strip():
            # Empty submission: judge will return an empty-report.
            new_result = {
                "judgment": None,
                "composite_score": None,
                "dimensions": {},
                "reasoning": "empty submission — skipped re-judge",
            }
        else:
            oracle = LLMAsAJudgeOracle(problem=problem, expected=problem.root_cause)
            new_result = oracle.evaluate(submission)
            if new_result.get("judgment") == "Error":
                errors.append(
                    f"{pid} exp={row['experiment']} attempt={row['attempt']}: "
                    f"{new_result.get('error') or new_result.get('reasoning')}"
                )

        old_composite = _safe_float(row.get("Diagnosis.composite_score", ""))
        old_d1 = _safe_float(row.get("D1", ""))
        old_d2 = _safe_float(row.get("D2", ""))
        old_d3 = _safe_float(row.get("D3", ""))

        new_composite = new_result.get("composite_score")
        new_dims = new_result.get("dimensions") or {}
        new_d1 = (new_dims.get("D1") or {}).get("score")
        new_d2 = (new_dims.get("D2") or {}).get("score")
        new_d3 = (new_dims.get("D3") or {}).get("score")

        delta = None
        if old_composite is not None and new_composite is not None:
            delta = round(new_composite - old_composite, 2)
            deltas.append(delta)
            if pid in EDITED_PROBLEMS:
                edited_deltas.append(delta)

        output_rows.append(
            {
                "experiment": row["experiment"],
                "problem_id": pid,
                "attempt": row["attempt"],
                "category": "edited" if pid in EDITED_PROBLEMS else "control",
                "old_success": row.get("Diagnosis.success"),
                "old_composite": old_composite,
                "old_D1": old_d1,
                "old_D2": old_d2,
                "old_D3": old_d3,
                "new_success": new_result.get("success"),
                "new_judgment": new_result.get("judgment"),
                "new_composite": new_composite,
                "new_D1": new_d1,
                "new_D2": new_d2,
                "new_D3": new_d3,
                "delta_composite": delta,
                "new_reasoning": new_result.get("reasoning"),
            }
        )

    fieldnames = list(output_rows[0].keys())
    out_path = FIXTURES_DIR / "stratus_rejudged.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    # Per-test reporting (visible with pytest -s)
    print(f"\nWrote {len(output_rows)} re-judged rows to {out_path}")
    if deltas:
        print(f"  all rows:    mean delta = {mean(deltas):+.3f} (n={len(deltas)})")
    if edited_deltas:
        print(f"  edited only: mean delta = {mean(edited_deltas):+.3f} (n={len(edited_deltas)})")

    # --- Assertions ---
    # 1. No row should have errored out.
    assert not errors, "Judge errors during re-judge:\n  " + "\n  ".join(errors)

    # 2. The fix should not regress edited problems on aggregate.
    #    Adding mechanism to ground truth can only help (or be neutral for genuinely
    #    wrong submissions); a negative aggregate delta would indicate a regression.
    #    Allow a small tolerance for judge noise.
    if edited_deltas:
        agg = mean(edited_deltas)
        assert agg >= -0.05, (
            f"Mean composite delta for edited problems is {agg:+.3f}, which "
            f"indicates a regression. Inspect {out_path} for per-row breakdown."
        )
