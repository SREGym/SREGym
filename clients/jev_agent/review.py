"""LLM review of the origin before the report (one call).

The decision model gathers evidence cheaply and ranks origin candidates, but it weighs some distinctions poorly
(a saturated component against the interaction that saturates it, a consumer against the record it is stuck on).
Here the LLM sees the top candidates with their evidence kinds and the notebook, and picks the one the
observations support best. It may only choose among the candidates; an invalid answer keeps the decision
model's choice.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from clients.jev_agent import evidence
from clients.jev_agent.notebook import Notebook
from clients.jev_agent.policy import CLUSTER_OBJECT, NOT_YET_KNOWN, InvestigationResult
from clients.jev_agent.types import AgentContext

logger = logging.getLogger("all.jev_agent.review")

MAX_CANDIDATES = 4
MAX_EVIDENCE_EXTRA = 2
STATE_BUDGET = 16000

SYSTEM = (
    "You review the conclusion of a Kubernetes incident investigation. `candidates` are the most likely origins "
    "of the fault ranked by a fast classifier, with `classifier_probability` (it can be wrong), the evidence kinds "
    "the observations show for each, and the lines behind them. `notebook` holds every observation. Choose the ONE "
    "candidate that is the origin: where the faulty change, defect, setting or input is, not where the symptoms "
    "show. A component that only fails because of something it depends on is a victim; a component that is only "
    "saturated or slow is where load lands, so ask what drives that load; a consumer stuck on one record points to "
    "that record (the input data); a change made shortly before the symptoms began that explains the failures outranks "
    "symptoms. "
    "Use only the observations. " + evidence.TRIAGE + " Answer with a JSON object only: "
    '{"origin": "<candidate id exactly as given>", "reason": "<one sentence citing observation ids>", '
    '"mechanism": "<one sentence: how the origin causes the failure>"}.'
)


@dataclass
class Review:
    """The LLM's second look at the origin: before and after, why, and the candidates it was shown."""

    before: str | None
    after: str | None
    reason: str = ""
    mechanism: str = ""
    candidates: list[str] = field(default_factory=list)
    error: str | None = None
    raw_text: str = ""


def candidates(
    inv: InvestigationResult, ctx: AgentContext | None = None, notebook: Notebook | None = None
) -> list[str]:
    """The classifier's top MAX_CANDIDATES, plus up to MAX_EVIDENCE_EXTRA others whose strongest evidence kind
    ranks above the strongest kind among those (the classifier can underrate a candidate the evidence favours)."""
    ranked = sorted(inv.origin_probs.items(), key=lambda kv: -kv[1])
    out = [k for k, _ in ranked if k != NOT_YET_KNOWN][:MAX_CANDIDATES]
    if inv.origin and inv.origin not in out:
        out = [inv.origin] + out[: MAX_CANDIDATES - 1]
    if ctx is None or notebook is None:
        return out

    def best_kind(key: str) -> int:
        """Rank in KINDS_ORDER of the candidate's strongest evidence kind beyond symptoms."""
        kinds = [
            k for k in evidence.candidate_evidence(ctx, notebook, key)[0] if k not in ("symptoms", "error_logs_only")
        ]
        return min((evidence.KINDS_ORDER.index(k) for k in kinds), default=len(evidence.KINDS_ORDER))

    top = min((best_kind(k) for k in out), default=len(evidence.KINDS_ORDER))
    extra = sorted(
        ((best_kind(k), -p, k) for k, p in ranked if k not in out and k not in (NOT_YET_KNOWN, CLUSTER_OBJECT)),
    )
    out += [k for rank, _, k in extra if rank < top][:MAX_EVIDENCE_EXTRA]
    return out


def review_origin(llm: Any, ctx: AgentContext, notebook: Notebook, inv: InvestigationResult) -> Review:
    """Never raises; returns the reviewed origin (or the original one with the error)."""
    keys = candidates(inv, ctx, notebook)
    result = Review(before=inv.origin, after=inv.origin, candidates=keys)
    if len(keys) < 2:
        result.error = "fewer than two candidates"
        return result
    items = []
    for key in keys:
        kinds, facts = evidence.candidate_evidence(ctx, notebook, key)
        items.append(
            {
                "id": key,
                "classifier_probability": round(inv.origin_probs.get(key, 0.0), 3),
                "evidence_kinds": kinds,
                "evidence_lines": facts,
                **({"note": "an object that is not a listed workload or object"} if key == CLUSTER_OBJECT else {}),
            }
        )
    payload = {"candidates": items, "notebook": notebook.state(STATE_BUDGET)}
    try:
        resp = llm.complete_json(SYSTEM, json.dumps(payload, ensure_ascii=False, default=str), max_output_tokens=1500)
    except Exception as exc:  # noqa: BLE001 - the review is optional
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.raw_text = (getattr(resp, "text", "") or "")[:2000]
    data = getattr(resp, "data", None)
    if data is None:
        from clients.jev_agent.llm import parse_json_object

        data = parse_json_object(result.raw_text)
    if not isinstance(data, dict) or data.get("origin") not in keys:
        result.error = f"answer not one of the candidates: {str(data)[:200]}"
        return result
    result.after = data["origin"]
    result.reason = str(data.get("reason") or "")[:400]
    result.mechanism = str(data.get("mechanism") or "")[:400]
    return result
