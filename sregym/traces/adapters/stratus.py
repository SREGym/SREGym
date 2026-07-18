"""Stratus -> ATIF v1.7 adapter.

Stratus is SREGym's own LangGraph agent, so \u2014 unlike claudecode/codex/opencode/
copilot \u2014 there is **no Harbor converter to port**. This adapter is bespoke,
built from Stratus's emitted trajectory format.

Input: ``<run_dir>/<ts>_<problem>_stratus_agent_trajectory.jsonl`` written by
``clients/stratus/stratus_agent/driver/driver.py::save_combined_trajectory``.
NDJSON:

    {"type":"metadata", "problem_id":..., "total_stages":N, "total_events":M, ...}
    {"type":"stage_start", "stage":"diagnosis", "num_events":K}
    {"type":"event", "stage":"diagnosis", "event_index":i, "num_steps":..,
     "submitted":bool, "rollback_stack":str, "messages":[...], "last_message":{...}}
    ... (more events, then more stages)

Key facts (confirmed against a real run):

- **Events are cumulative snapshots.** Stratus streams LangGraph state with
  ``stream_mode="values"``, so each event's ``messages`` is the full history so
  far. The **last event of each stage** holds that stage's complete history \u2014 we
  convert only that, per stage (iterating every snapshot would duplicate).
- **Stages are sequential phases** (``diagnosis``, ``mitigation_attempt_0``, ...).
  We concatenate them into one ATIF trajectory and record per-stage boundaries
  under ``extra.sregym.stages``.
- **Messages** are LangChain-serialized: ``type`` is the class name
  (``SystemMessage`` / ``HumanMessage`` / ``AIMessage`` / ``ToolMessage``),
  ``content`` is the text, ``AIMessage.tool_calls`` = ``[{name, args, id}]``.
- **Tool results** (``ToolMessage``) carry ``tool_call_id`` when the emitter
  preserves it (post-fix runs); older runs lack it, so we fall back to
  **positional** matching (the N ToolMessages after an AIMessage map to its N
  tool_calls in order).
- **Token usage**: ``AIMessage.usage_metadata`` (input/output/total tokens) is
  serialized post-fix and mapped to ATIF ``Metrics``; absent on older runs
  (metrics simply omitted).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sregym.traces.adapters._common import _load_jsonl, _stringify
from sregym.traces.atif import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "stratus"


# --------------------------------------------------------------------------- #
# trajectory-file discovery
# --------------------------------------------------------------------------- #
def _find_trajectory_file(run_dir: Path) -> Path | None:
    """Locate the Stratus trajectory JSONL within a run directory (newest if several)."""
    candidates = sorted(run_dir.glob("*_stratus_agent_trajectory.jsonl"))
    if not candidates:
        return None
    # Newest by name (filenames are timestamp-prefixed) / mtime as tiebreak.
    return max(candidates, key=lambda p: (p.name, p.stat().st_mtime))


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def _metrics_from_usage(usage: dict[str, Any] | None, resp_meta: dict[str, Any] | None) -> Metrics | None:
    """Map a LangChain ``usage_metadata`` dict to ATIF ``Metrics`` (or None)."""
    if not isinstance(usage, dict) or not usage:
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cached = None
    details = usage.get("input_token_details")
    if isinstance(details, dict):
        cached = details.get("cache_read")
    extra: dict[str, Any] = {}
    if resp_meta:
        # Keep model/cost breadcrumbs if the provider reported them.
        for key in ("model_name", "model", "finish_reason"):
            if resp_meta.get(key) is not None:
                extra[key] = resp_meta[key]
    if input_tokens is None and output_tokens is None and cached is None:
        return None
    return Metrics(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        cached_tokens=cached,
        extra=extra or None,
    )


# --------------------------------------------------------------------------- #
# message -> step conversion
# --------------------------------------------------------------------------- #
def _msg_text(msg: dict[str, Any]) -> str:
    """Flatten a serialized message's content to text."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", "") or _stringify(block))
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return _stringify(content)


def _tool_calls_from_ai(msg: dict[str, Any]) -> list[ToolCall]:
    """Extract ATIF ToolCalls from a serialized AIMessage."""
    calls: list[ToolCall] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        call_id = tc.get("id") or f"stratus_call_{i}"
        args = tc.get("args")
        if not isinstance(args, dict):
            args = {} if args is None else {"value": args}
        calls.append(
            ToolCall(
                tool_call_id=call_id,
                function_name=tc.get("name") or "",
                arguments=args,
            )
        )
    return calls


def _convert_messages(messages: list[dict[str, Any]], start_step_id: int) -> tuple[list[Step], int]:
    """Convert a stage's serialized messages into ATIF steps.

    Tool results are attached to the issuing agent step by ``tool_call_id`` when
    present, else positionally (fills the most recent agent step's unmatched
    tool-call slots in order). Returns (steps, next_step_id).
    """
    steps: list[Step] = []
    step_id = start_step_id
    # id -> (step, set of already-filled call ids) for id-based matching.
    call_owner: dict[str, Step] = {}
    # For positional fallback: queue of (step, [unmatched call ids]) in emission order.
    pending_positional: list[tuple[Step, list[str]]] = []

    def _new_step(**kw: Any) -> Step:
        nonlocal step_id
        s = Step(step_id=step_id, **kw)
        steps.append(s)
        step_id += 1
        return s

    for msg in messages:
        mtype = msg.get("type")

        if mtype == "SystemMessage":
            _new_step(source="system", message=_msg_text(msg))

        elif mtype == "HumanMessage":
            _new_step(source="user", message=_msg_text(msg))

        elif mtype == "AIMessage":
            tool_calls = _tool_calls_from_ai(msg)
            step = _new_step(
                source="agent",
                message=_msg_text(msg),
                tool_calls=tool_calls or None,
                metrics=_metrics_from_usage(msg.get("usage_metadata"), msg.get("response_metadata")),
                llm_call_count=1,
            )
            if tool_calls:
                ids = [tc.tool_call_id for tc in tool_calls]
                for cid in ids:
                    call_owner[cid] = step
                pending_positional.append((step, list(ids)))

        elif mtype == "ToolMessage":
            result = ObservationResult(content=_msg_text(msg) or None)
            call_id = msg.get("tool_call_id")
            owner: Step | None = None
            if call_id and call_id in call_owner:
                # id-based match (post-fix runs)
                owner = call_owner[call_id]
                result.source_call_id = call_id
                # remove from positional queue too
                for _step, ids in pending_positional:
                    if _step is owner and call_id in ids:
                        ids.remove(call_id)
                        break
            else:
                # positional fallback: earliest agent step with an unfilled slot
                for _step, ids in pending_positional:
                    if ids:
                        owner = _step
                        result.source_call_id = ids.pop(0)
                        break
            if msg.get("name"):
                result.extra = {"tool_name": msg["name"]}
            if owner is not None:
                if owner.observation is None:
                    owner.observation = Observation(results=[result])
                else:
                    owner.observation.results.append(result)
            else:
                # No issuing call found: keep as its own step rather than drop.
                _new_step(source="user", message=_msg_text(msg) or "Tool result")

    return steps, step_id


# --------------------------------------------------------------------------- #
# main conversion
# --------------------------------------------------------------------------- #
def _convert(records: list[dict[str, Any]]) -> tuple[list[Step], list[dict[str, Any]]] | None:
    """Build ATIF steps from parsed trajectory records.

    Returns (steps, stage_summaries) or None if nothing convertible.
    """
    # Group events by stage in first-seen order.
    stage_order: list[str] = []
    events_by_stage: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("type") != "event":
            continue
        stage = rec.get("stage", "unknown")
        if stage not in events_by_stage:
            events_by_stage[stage] = []
            stage_order.append(stage)
        events_by_stage[stage].append(rec)

    if not stage_order:
        return None

    steps: list[Step] = []
    stage_summaries: list[dict[str, Any]] = []
    next_id = 1

    for stage in stage_order:
        events = events_by_stage[stage]
        # Cumulative snapshots -> the last event holds the full stage history.
        last = max(events, key=lambda e: e.get("event_index", 0))
        messages = last.get("messages") or []
        first_step = next_id
        stage_steps, next_id = _convert_messages(messages, next_id)
        steps.extend(stage_steps)
        stage_summaries.append(
            {
                "stage": stage,
                "first_step": first_step,
                "last_step": next_id - 1,
                "num_steps": last.get("num_steps"),
                "submitted": bool(last.get("submitted", False)),
            }
        )

    if not steps:
        return None
    return steps, stage_summaries


def _aggregate_final_metrics(steps: list[Step]) -> FinalMetrics | None:
    prompt = [s.metrics.prompt_tokens for s in steps if s.metrics and s.metrics.prompt_tokens is not None]
    completion = [s.metrics.completion_tokens for s in steps if s.metrics and s.metrics.completion_tokens is not None]
    cached = [s.metrics.cached_tokens for s in steps if s.metrics and s.metrics.cached_tokens is not None]
    return FinalMetrics(
        total_prompt_tokens=sum(prompt) if prompt else None,
        total_completion_tokens=sum(completion) if completion else None,
        total_cached_tokens=sum(cached) if cached else None,
        total_cost_usd=None,
        total_steps=len(steps),
    )


def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert a Stratus run directory into a validated ATIF ``Trajectory``.

    Args:
        run_dir: Canonical run directory
            (``results/<batch>/stratus/<problem_id>/run_<n>/``) containing a
            ``*_stratus_agent_trajectory.jsonl``.
        sregym_meta: Optional SREGym metadata to attach under ``extra.sregym``;
            per-stage boundaries are added under ``extra.sregym.stages``.

    Returns:
        A validated ``Trajectory``, or ``None`` if no convertible trajectory exists.
    """
    run_dir = Path(run_dir)
    traj_file = _find_trajectory_file(run_dir)
    if traj_file is None:
        logger.debug("No Stratus trajectory JSONL found in %s", run_dir)
        return None

    records = _load_jsonl(traj_file)
    if not records:
        return None

    metadata = next((r for r in records if r.get("type") == "metadata"), {})

    converted = _convert(records)
    if converted is None:
        logger.debug("No convertible events in Stratus trajectory %s", traj_file)
        return None
    steps, stage_summaries = converted

    sregym: dict[str, Any] = dict(sregym_meta or {})
    sregym["stages"] = stage_summaries
    # Run-level submitted = any stage submitted.
    sregym.setdefault("submitted", any(s["submitted"] for s in stage_summaries))
    # The diagnosis->mitigation boundary is explicit in Stratus's stage structure
    # (the generic "Submission received" marker doesn't apply — Stratus's submit
    # tool emits "Submission complete"). Record the last step of the diagnosis
    # stage as the boundary so consumers can split the two phases.
    diagnosis = next((s for s in stage_summaries if s["stage"] == "diagnosis"), None)
    if diagnosis is not None:
        sregym.setdefault("diagnosis_submitted_step", diagnosis["last_step"])

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=metadata.get("problem_id"),
        agent=Agent(name=AGENT_NAME, version="unknown"),
        steps=steps,
        final_metrics=_aggregate_final_metrics(steps),
        extra={"sregym": sregym},
    )
