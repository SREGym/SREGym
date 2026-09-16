"""CloudThinker -> ATIF v1.7 adapter.

CloudThinker is a hosted multi-agent system driven through its own CLI, so --
unlike claudecode/codex/opencode/copilot -- there is **no Harbor converter to
port**. This adapter is bespoke, built from the session file the CloudThinker
SREGym driver writes (``clients/cloudthinker/driver.py::write_session``).

Input: ``cloudthinker_session.json`` in the run directory.

    {"schema": "cloudthinker_session/v1",
     "problem_id": ..., "agent": "cloudthinker", "selection": ...,
     "prompt_mode": ..., "memory_arm": ...,
     "stages": [{"name": "diagnosis", "conversation_id": ..., "submitted": ...,
                 "rows_at_submission": N}, ...],
     "conversations": [{"conversation_id": ..., "records": [row, ...]}]}

Each record is one message component read from the platform's own tables:
``role`` is ``user`` or the assistant's name, ``component_type`` is TEXT,
THINKING or TOOL, and the TOOL columns carry
``tool_name``/``tool_input``/``tool_output``/``tool_reasoning``/``tool_runtime``.

Key facts (confirmed against a real run):

- **A stage resumes the previous stage's conversation.** The mitigation turn
  continues the diagnosis conversation, so the transcript is converted once per
  conversation and split into stages by ``rows_at_submission`` -- the row count
  the driver recorded when that stage submitted. A stage without a watermark
  takes everything left in its conversation.
- **User text lives on the message row**, not in a component: a user message
  owns no component row, so ``message_content`` is the step text whenever
  ``component_type`` is null.
- **One agent message is one step.** Its TEXT components are the message, its
  THINKING component is the reasoning, and its TOOL components are the tool calls
  whose outputs attach as a single observation.
- **The tables carry no token usage**, so per-step metrics are omitted and the
  aggregate carries only the step count.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..atif import (
    Agent,
    FinalMetrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "cloudthinker"
SESSION_SCHEMA = "cloudthinker_session/v1"
SESSION_FILENAME = "cloudthinker_session.json"

_USER_ROLE = "user"
_TEXT = "TEXT"
_THINKING = "THINKING"
_TOOL = "TOOL"


def _clean(value: Any) -> str:
    """Text of a component column, or "" -- never the string "None"."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


def _arguments(value: Any) -> dict[str, Any]:
    """ATIF tool arguments, which must be an object."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"value": value}
        if isinstance(decoded, dict):
            return decoded
        return {"value": decoded}
    return {}


def _tool_extra(row: dict[str, Any]) -> dict[str, Any] | None:
    extra: dict[str, Any] = {}
    reasoning = _clean(row.get("tool_reasoning"))
    if reasoning:
        extra["reasoning"] = reasoning
    if row.get("tool_runtime") is not None:
        extra["runtime_s"] = row["tool_runtime"]
    if row.get("approval_status"):
        extra["approval_status"] = row["approval_status"]
    if row.get("is_completed") is False:
        extra["is_completed"] = False
    return extra or None


def _result_extra(row: dict[str, Any]) -> dict[str, Any] | None:
    extra: dict[str, Any] = {}
    if row.get("tool_name"):
        extra["tool_name"] = row["tool_name"]
    if row.get("tool_runtime") is not None:
        extra["runtime_s"] = row["tool_runtime"]
    if row.get("approval_status"):
        extra["approval_status"] = row["approval_status"]
    if row.get("is_completed") is False:
        extra["is_completed"] = False
    return extra or None


def _group_by_message(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Rows grouped by owning message, in first-seen order.

    The SQL orders by ``(created_at, position)``, so first-seen order is
    conversation order; several rows of one message are its components.
    """
    groups: list[list[dict[str, Any]]] = []
    index: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        if not isinstance(row, dict):
            continue
        key = str(row.get("message_id") or f"row-{len(groups)}-{len(index)}")
        group = index.get(key)
        if group is None:
            group = []
            index[key] = group
            groups.append(group)
        group.append(row)
    return groups


def _steps_from_records(records: list[dict[str, Any]], start_step_id: int) -> list[Step]:
    """One conversation's rows -> ATIF steps, ids sequential from ``start_step_id``."""
    steps: list[Step] = []
    step_id = start_step_id

    for rows in _group_by_message(records):
        head = rows[0]
        role = _clean(head.get("role")).lower()
        timestamp = head.get("created_at")

        if role == _USER_ROLE:
            text = ""
            for row in rows:
                text = _clean(row.get("message_content")) or _clean(row.get("text"))
                if text:
                    break
            if not text:
                continue
            steps.append(Step(step_id=step_id, source="user", message=text, timestamp=timestamp))
            step_id += 1
            continue

        text_parts = [_clean(row.get("text")) for row in rows if _clean(row.get("component_type")).upper() == _TEXT]
        thinking = [
            _clean(row.get("thinking")) for row in rows if _clean(row.get("component_type")).upper() == _THINKING
        ]
        tool_rows = [row for row in rows if _clean(row.get("tool_name"))]

        tool_calls: list[ToolCall] = []
        results: list[ObservationResult] = []
        for position, row in enumerate(tool_rows):
            call_id = _clean(row.get("tool_call_id")) or f"{AGENT_NAME}_call_{step_id}_{position}"
            tool_calls.append(
                ToolCall(
                    tool_call_id=call_id,
                    function_name=_clean(row.get("tool_name")),
                    arguments=_arguments(row.get("tool_input")),
                    extra=_tool_extra(row),
                )
            )
            results.append(
                ObservationResult(
                    source_call_id=call_id,
                    content=_clean(row.get("tool_output")) or None,
                    extra=_result_extra(row),
                )
            )

        message = "\n".join(part for part in text_parts if part)
        reasoning = "\n".join(part for part in thinking if part)
        if not message and not reasoning and not tool_calls:
            # A component row with nothing on it (e.g. an interrupted turn) is
            # not a step; emitting an empty one would shift every later step_id.
            continue

        steps.append(
            Step(
                step_id=step_id,
                source="agent",
                message=message,
                reasoning_content=reasoning or None,
                tool_calls=tool_calls or None,
                observation=Observation(results=results) if results else None,
                llm_call_count=1,
                timestamp=timestamp,
            )
        )
        step_id += 1

    return steps


def _convert(session: dict[str, Any]) -> tuple[list[Step], list[dict[str, Any]]] | None:
    """Split the session's conversations into staged ATIF steps.

    Returns (steps, stage_summaries) or None when nothing is convertible.
    """
    stages = session.get("stages")
    if not isinstance(stages, list) or not stages:
        return None

    conversations: dict[str, list[dict[str, Any]]] = {}
    for entry in session.get("conversations") or []:
        if not isinstance(entry, dict) or not entry.get("conversation_id"):
            continue
        records = entry.get("records")
        conversations[str(entry["conversation_id"])] = records if isinstance(records, list) else []

    steps: list[Step] = []
    summaries: list[dict[str, Any]] = []
    consumed: dict[str, int] = {}

    for stage in stages:
        if not isinstance(stage, dict):
            continue
        conversation_id = str(stage.get("conversation_id") or "")
        rows = conversations.get(conversation_id, [])
        start = consumed.get(conversation_id, 0)
        watermark = stage.get("rows_at_submission")
        end = len(rows) if not isinstance(watermark, int) else watermark
        end = max(start, min(end, len(rows)))
        consumed[conversation_id] = end

        chunk = rows[start:end]
        first_step = len(steps) + 1
        steps.extend(_steps_from_records(chunk, first_step))
        summaries.append(
            {
                "stage": stage.get("name"),
                "conversation_id": conversation_id or None,
                "first_step": first_step if len(steps) >= first_step else None,
                "last_step": len(steps) if len(steps) >= first_step else None,
                "rows": len(chunk),
                "submitted": bool(stage.get("submitted")),
            }
        )

    if not steps:
        return None
    return steps, summaries


def _final_metrics(steps: list[Step]) -> FinalMetrics:
    """Step count only: these tables carry no token usage."""
    return FinalMetrics(total_steps=len(steps))


def convert_file(session_file: Path | str) -> Trajectory | None:
    """Convert one CloudThinker session file to ATIF."""
    path = Path(session_file)
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Could not read CloudThinker session %s: %s", path, exc)
        return None
    if not isinstance(session, dict) or session.get("schema") != SESSION_SCHEMA:
        return None

    converted = _convert(session)
    if converted is None:
        logger.debug("No convertible messages in CloudThinker session %s", path)
        return None
    steps, summaries = converted

    meta: dict[str, Any] = {
        "stages": summaries,
        "selection": session.get("selection") or None,
        "prompt_mode": session.get("prompt_mode") or None,
        "memory_arm": session.get("memory_arm") or None,
    }
    # The diagnosis boundary is the last step of the diagnosis stage, unless that
    # stage is the whole run. The generic "Submission received" scan does not
    # apply: the driver submits to the conductor, not through a tool call.
    if len(summaries) > 1 and summaries[0].get("last_step"):
        meta["diagnosis_submitted_step"] = summaries[0]["last_step"]

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session.get("problem_id") or None,
        agent=Agent(name=AGENT_NAME, version="unknown", model_name=session.get("selection") or None),
        steps=steps,
        final_metrics=_final_metrics(steps),
        extra={"cloudthinker": meta},
    )
