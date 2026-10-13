"""One-shot LLM escalation when the decision-model loop stalls (at most once per run).

Triggers (checked after each decision step, see `trigger`):
* evidence is still far from enough (enough_evidence < ENOUGH_LOW) after ESC_MIN_STEPS tool steps;
* the last STALE_STEPS judged observations were all judged normal (last_result_abnormal < 0.5);
* origin is still unclear (top real-origin probability < ORIGIN_P_MIN) after half the step budget.

The LLM sees the notebook state, the tool menu, and the valid targets, and must answer strict JSON:
{"hypotheses":[{"component","mechanism","supporting":["O3"],"test":{"tool","target"}}],
 "suggested_actions":[{"tool","target","reason"}]}
Everything it returns is validated against the menu: off-menu tools, unknown targets, unknown observation ids
are dropped. Hypotheses go into the notebook (H1..H3); suggested actions are pinned at the top of the options.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

# The next_action distribution is spread over ~40 options, so its top probability or entropy says little about
# being stuck (it was below 0.25 on most early steps); stalls are judged from the evidence questions instead.
ENOUGH_LOW = 0.3
ESC_MIN_STEPS = 8
STALE_STEPS = 3
ORIGIN_P_MIN = 0.4
MAX_HYPOTHESES = 3
MAX_SUGGESTIONS = 3

SYSTEM_PROMPT = (
    "You assist a Kubernetes incident investigation. A decision model chooses evidence-gathering actions from a "
    "fixed menu; it is stuck. Read the notebook (application, overview of components, observations O1..On), "
    "propose up to 3 hypotheses about which component or object is the origin of the fault and by what "
    "mechanism, and the actions that would best confirm or refute them. Use only tools and targets from the "
    "menu, exactly as written. Do not suggest actions already taken. Answer with a JSON object only, in this "
    'shape: {"hypotheses":[{"component":"<component id or cluster object>","mechanism":"<one sentence>",'
    '"supporting":["O3"],"test":{"tool":"<tool>","target":"<target>"}}],'
    '"suggested_actions":[{"tool":"<tool>","target":"<target>","reason":"<one sentence>"}]}'
)


def normalized_entropy(probs: dict[str, float]) -> float:
    """Entropy of a probability table scaled to 0..1 (1 = all options equally likely)."""
    values = [p for p in probs.values() if p > 0]
    n = len(probs)
    if n <= 1:
        return 0.0
    total = sum(values) or 1.0
    h = -sum((p / total) * math.log(p / total) for p in values)
    return h / math.log(n)


def trigger(steps: list[dict], *, tool_steps: int, max_steps: int) -> str | None:
    """The reason to escalate now, or None. `steps` are per-step summaries with next_p, next_entropy,
    last_abnormal, origin_real_p (see policy.Investigator)."""
    if not steps:
        return None
    cur = steps[-1]
    if tool_steps >= ESC_MIN_STEPS and cur.get("enough") is not None and cur["enough"] < ENOUGH_LOW:
        return f"enough_evidence {cur['enough']:.2f} < {ENOUGH_LOW} after {tool_steps} steps"
    judged = [s["last_abnormal"] for s in steps if s.get("last_abnormal") is not None]
    if len(judged) >= STALE_STEPS and all(p < 0.5 for p in judged[-STALE_STEPS:]):
        return f"last {STALE_STEPS} observations judged normal"
    if tool_steps >= max_steps / 2 and (cur.get("origin_real_p") or 0.0) < ORIGIN_P_MIN:
        return f"origin top probability {cur.get('origin_real_p') or 0.0:.2f} < {ORIGIN_P_MIN} after half the budget"
    return None


@dataclass
class EscalationResult:
    """The LLM's answer when the investigation is stuck: hypotheses and suggested checks from the menu."""

    reason: str
    hypotheses: list[dict] = field(default_factory=list)
    suggested: list[dict] = field(default_factory=list)  # {"tool","target","reason"}
    dropped: list[str] = field(default_factory=list)
    error: str | None = None
    usage: dict = field(default_factory=dict)
    raw_text: str = ""

    def to_dict(self) -> dict:
        """The result as a plain dict (for the trace)."""
        return {
            "reason": self.reason,
            "hypotheses": self.hypotheses,
            "suggested": self.suggested,
            "dropped": self.dropped,
            "error": self.error,
            "usage": self.usage,
        }


def _valid_action(item: Any, menu: dict[str, set[str]], done: set[str]) -> tuple[str, str] | None:
    """(tool, target) of a suggested action that is on the menu and not done yet, else None."""
    if not isinstance(item, dict):
        return None
    tool, target = str(item.get("tool") or "").strip(), str(item.get("target") or "").strip()
    if tool not in menu or target not in menu[tool] or f"{tool}:{target}" in done:
        return None
    return tool, target


def validate(
    payload: Any, menu: dict[str, set[str]], *, observation_ids: set[str], done: set[str] = frozenset()
) -> tuple[list[dict], list[dict], list[str]]:
    """Return (hypotheses, suggested_actions, dropped) with everything off-menu removed."""
    dropped: list[str] = []
    hypotheses: list[dict] = []
    suggested: list[dict] = []
    seen: set[str] = set()
    if not isinstance(payload, dict):
        return [], [], ["payload is not a JSON object"]
    for item in payload.get("suggested_actions") or []:
        ok = _valid_action(item, menu, done)
        if ok is None:
            dropped.append(f"suggested_action {json.dumps(item, default=str)[:160]}")
            continue
        key = f"{ok[0]}:{ok[1]}"
        if key in seen or len(suggested) >= MAX_SUGGESTIONS:
            continue
        seen.add(key)
        suggested.append({"tool": ok[0], "target": ok[1], "reason": str(item.get("reason") or "")[:200]})
    for item in payload.get("hypotheses") or []:
        if len(hypotheses) >= MAX_HYPOTHESES:
            break
        if not isinstance(item, dict) or not str(item.get("component") or "").strip():
            dropped.append(f"hypothesis {json.dumps(item, default=str)[:160]}")
            continue
        test = _valid_action(item.get("test"), menu, done)
        if item.get("test") and test is None:
            dropped.append(f"hypothesis test {json.dumps(item.get('test'), default=str)[:160]}")
        supporting = [s for s in item.get("supporting") or [] if isinstance(s, str) and s in observation_ids]
        hyp = {
            "component": str(item["component"]).strip()[:120],
            "mechanism": str(item.get("mechanism") or "").strip()[:300],
            "supporting": supporting,
            "test": f"{test[0]}:{test[1]}" if test else None,
        }
        hypotheses.append(hyp)
        if test and f"{test[0]}:{test[1]}" not in seen and len(suggested) < MAX_SUGGESTIONS:
            seen.add(f"{test[0]}:{test[1]}")
            suggested.append(
                {"tool": test[0], "target": test[1], "reason": f"tests hypothesis: {hyp['mechanism']}"[:200]}
            )
    return hypotheses, suggested, dropped


def tool_menu(registry: dict, menu: dict[str, set[str]]) -> list[dict]:
    """The tools the LLM may suggest, each with what it reveals and its valid targets."""
    return [
        {
            "tool": name,
            "target_kind": spec.target_kind,
            "reveals": spec.reveals,
            "valid_targets": sorted(menu.get(name, ())),
        }
        for name, spec in registry.items()
        if menu.get(name)
    ]


def escalate(
    llm: Any,
    *,
    reason: str,
    notebook_state: dict,
    registry: dict,
    menu: dict[str, set[str]],
    observation_ids: set[str],
    done: set[str],
) -> EscalationResult:
    """Ask the LLM once; never raises (errors are reported in the result)."""
    result = EscalationResult(reason=reason)
    user = json.dumps(
        {
            "why_escalated": reason,
            "notebook": notebook_state,
            "tool_menu": tool_menu(registry, menu),
            "actions_already_taken": sorted(done),
        },
        ensure_ascii=False,
        default=str,
    )
    try:
        resp = llm.complete_json(SYSTEM_PROMPT, user, max_output_tokens=3000)
    except Exception as exc:  # noqa: BLE001 - escalation is optional
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.usage = dict(getattr(resp, "usage", {}) or {})
    result.raw_text = getattr(resp, "text", "") or ""
    data = getattr(resp, "data", None)
    if data is None:
        from clients.jev_agent.llm import parse_json_object

        data = parse_json_object(result.raw_text)
    if data is None:
        result.error = "LLM answer is not a JSON object"
        return result
    result.hypotheses, result.suggested, result.dropped = validate(
        data, menu, observation_ids=observation_ids, done=done
    )
    return result
