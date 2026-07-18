"""Copilot CLI -> ATIF v1.7 adapter.

A clean port of Harbor's ``CopilotCli._convert_jsonl_to_trajectory`` and its
helpers (upstream commit ``fd1a8ea``, ``src/harbor/agents/installed/copilot_cli.py``;
see ``sregym/traces/atif/UPSTREAM.md``) into standalone, pure functions
with no dependency on ``harbor`` or ``BaseInstalledAgent``.

The conversion reads the Copilot CLI **JSONL** output (``copilot-cli.jsonl``,
produced by ``copilot --output-format json`` and captured by the copilot
client) and produces one validated ATIF ``Trajectory``.

``copilot --output-format json`` emits one of two event shapes depending on the
underlying model, and both are handled side by side:

- **Flat schema** (Anthropic models): ``message`` / ``tool_use`` /
  ``tool_result`` / ``usage`` events.
- **Session-event schema** (OpenAI/GPT models): namespaced ``assistant.message``
  / ``user.message`` / ``tool.execution_start`` / ``tool.execution_complete`` /
  ``result`` events with the payload wrapped in a ``data`` object.

Key behavior ported from Harbor:

- **tool-result matching**: a tool result arrives in a separate event from its
  call, so each issuing step is registered by tool-call id (``call_id_map``) and
  the result is matched back to it (position is unreliable — a turn's parallel
  results may be interleaved).
- **stderr tolerance**: the run merges stderr into the JSONL file (``2>&1``), so
  non-JSON lines are skipped (and counted) rather than treated as an error.
- **salvage**: an event that fails to convert is preserved as a minimal step
  (raw payload + parse error under ``extra``) instead of being dropped.
- **cost**: Copilot's flat/GPT streams do not report a per-run USD cost, so
  ``total_cost_usd`` is left unset; the terminal ``result`` summary is preserved
  under ``final_metrics.extra['copilot_result']``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

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

AGENT_NAME = "copilot"

# The client captures the JSON stream here (see clients/copilot/copilot_agent.py).
_JSONL_FILENAME = "copilot-cli.jsonl"


# --------------------------------------------------------------------------- #
# Content extraction helpers (ported verbatim from Harbor's static methods)
# --------------------------------------------------------------------------- #
def _flatten_content(content: Any) -> str:
    """Flatten string-or-multimodal message content to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    if isinstance(content, dict):
        text = content.get("text", "")
        return text if isinstance(text, str) else str(content)
    return str(content)


def _normalize_tool_arguments(arguments: Any) -> dict[str, Any]:
    """Normalize a tool call's arguments to a dict.

    Copilot sends a dict for most tools but a raw string for some (e.g.
    ``apply_patch``'s patch text); the string is preserved under ``value``
    rather than dropped.
    """
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    return {"value": arguments}


def _stringify_tool_result(result: Any) -> str:
    """Reduce a ``tool.execution_complete`` result (or ``error``) to text.

    A recognized text key supplies the human-readable body, but the remaining
    keys (e.g. ``stderr``, ``exitCode``, an error ``code``) are appended as a
    JSON tail so siblings are preserved rather than dropped. With no text key the
    whole object is dumped.
    """
    if isinstance(result, dict):
        body = ""
        body_key = None
        for key in ("content", "output", "stdout", "text", "message"):
            value = result.get(key)
            if isinstance(value, str) and value:
                body, body_key = value, key
                break
        if not body:
            return json.dumps(result, ensure_ascii=False)
        remainder = {k: v for k, v in result.items() if k != body_key}
        if remainder:
            return f"{body}\n{json.dumps(remainder, ensure_ascii=False)}"
        return body
    return _flatten_content(result)


def _record_tool_result(
    call_id_map: dict[str, Step],
    steps: list[Step],
    step_id: int,
    *,
    call_id: str | None,
    content: str,
    timestamp: str | None,
    extra: dict[str, Any] | None = None,
) -> int:
    """Attach a tool result to the step that issued the matching call.

    The result is matched back to the issuing step by id (a turn's parallel
    results may be interleaved or reordered, so position can't be relied on). A
    result with no matching call is kept as its own step rather than dropped.
    ``extra`` carries any execution metadata so it is preserved. Returns the next
    step id.
    """
    result = ObservationResult(
        source_call_id=call_id or None,
        content=content or None,
        extra=extra,
    )
    owner = call_id_map.get(call_id) if call_id else None
    if owner is not None:
        if owner.observation is None:
            owner.observation = Observation(results=[result])
        else:
            owner.observation.results.append(result)
        return step_id
    # No issuing step to attach to: keep the result as its own step. Fold the id
    # into ``extra`` (a standalone step has no ``source_call_id`` field) rather
    # than dropping the only copy.
    if call_id:
        extra = {"source_call_id": call_id, **(extra or {})}
    steps.append(
        Step(
            step_id=step_id,
            timestamp=timestamp,
            source="agent",
            message=content or "Tool result",
            extra=extra,
        )
    )
    return step_id + 1


def _record_unparsed_event(
    steps: list[Step],
    step_id: int,
    event: dict[str, Any],
    error: Exception,
) -> int:
    """Preserve an event that failed to convert as a minimal salvage step.

    The raw payload and parse error are kept under ``extra`` (and any recoverable
    text becomes the message), so partial data survives and is merely flagged.
    Built only from safe values so the salvage itself cannot raise. Returns the
    next step id.
    """
    event_type = event.get("type")
    source = "user" if isinstance(event_type, str) and event_type.startswith("user") else "agent"
    raw_content = event.get("content")
    if raw_content is None and isinstance(event.get("data"), dict):
        raw_content = event["data"].get("content")
    try:
        message = _flatten_content(raw_content) or "[unparsed Copilot event]"
    except Exception:
        message = "[unparsed Copilot event]"
    try:
        steps.append(
            Step(
                step_id=step_id,
                source=source,
                message=message,
                extra={"copilot_parse_error": str(error), "raw_event": event},
            )
        )
        return step_id + 1
    except Exception:
        logger.debug("Could not salvage malformed Copilot event", exc_info=True)
        return step_id


# --------------------------------------------------------------------------- #
# JSONL reader (ported from Harbor's _read_copilot_cli_jsonl)
# --------------------------------------------------------------------------- #
def _read_copilot_cli_jsonl(jsonl_path: Path) -> list[dict[str, Any]]:
    """Read and parse the Copilot CLI JSONL output file.

    Skips non-JSON lines (the run merges stderr into this file via ``2>&1``) and
    logs a single sample so the loss is observable. Never raises.
    """
    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except OSError as ex:
        logger.debug("Error reading Copilot CLI trajectory file %s: %s", jsonl_path, ex)
        return []

    if text.startswith("Error: No authentication information found"):
        logger.error("Copilot CLI authentication error:\n%s", text)
        return []

    raw_events: list[dict[str, Any]] = []
    dropped_count = 0
    first_dropped: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw_events.append(json.loads(line))
        except json.JSONDecodeError:
            dropped_count += 1
            if first_dropped is None:
                first_dropped = line
            continue

    if dropped_count:
        logger.debug(
            "Skipped %d non-JSON line(s) in Copilot CLI output file %s. First (truncated): %s",
            dropped_count,
            jsonl_path,
            (first_dropped or "")[:500],
        )
    if not raw_events and text.strip():
        logger.debug(
            "Copilot CLI output file %s contained no valid JSON. Raw content (first 500 chars): %s",
            jsonl_path,
            text[:500],
        )
    return raw_events


# --------------------------------------------------------------------------- #
# Main conversion (ported from Harbor's _convert_jsonl_to_trajectory)
# --------------------------------------------------------------------------- #
def _convert_events(raw_events: list[dict[str, Any]]) -> Trajectory | None:
    """Convert parsed Copilot CLI JSONL events into an ATIF ``Trajectory``."""
    if not raw_events:
        return None

    state = {"step_id": 1, "in": 0, "out": 0, "result": None}
    steps: list[Step] = []
    call_id_map: dict[str, Step] = {}
    skipped_event_types: dict[str, int] = {}
    failed_events = 0

    def _handle_event(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        timestamp = event.get("timestamp")

        # --- message (flat schema) ---
        if event_type == "message":
            role = event.get("role", "user")
            source = "agent" if role == "assistant" else "user"
            step = Step(
                step_id=state["step_id"],
                timestamp=timestamp,
                source=source,
                message=_flatten_content(event.get("content", "")),
            )
            model_name_val = event.get("model")
            if source == "agent" and model_name_val:
                step.model_name = model_name_val
            steps.append(step)
            state["step_id"] += 1

        # --- tool_use (flat schema) ---
        elif event_type == "tool_use":
            tool_name = event.get("name", "")
            tool_call = ToolCall(
                tool_call_id=event.get("id", ""),
                function_name=tool_name,
                arguments=_normalize_tool_arguments(event.get("input", {})),
            )
            step = Step(
                step_id=state["step_id"],
                timestamp=timestamp,
                source="agent",
                message=f"Executed {tool_name}",
                tool_calls=[tool_call],
            )
            if model_name_val := event.get("model"):
                step.model_name = model_name_val
            steps.append(step)
            state["step_id"] += 1
            if tool_call.tool_call_id:
                call_id_map[tool_call.tool_call_id] = step

        # --- tool_result (flat schema) ---
        elif event_type == "tool_result":
            tr_extra = {
                k: v for k, v in event.items() if k not in ("type", "timestamp", "tool_use_id", "content")
            } or None
            state["step_id"] = _record_tool_result(
                call_id_map,
                steps,
                state["step_id"],
                call_id=event.get("tool_use_id"),
                content=_flatten_content(event.get("content")),
                timestamp=timestamp,
                extra=tr_extra,
            )

        # --- usage (flat schema) ---
        elif event_type == "usage":
            input_tokens = event.get("input_tokens", 0)
            output_tokens = event.get("output_tokens", 0)
            state["in"] += input_tokens
            state["out"] += output_tokens
            if steps and steps[-1].source == "agent":
                steps[-1].metrics = Metrics(
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                )

        # --- assistant.message (session-event schema) ---
        elif event_type == "assistant.message":
            data = event["data"]
            raw_content = data.get("content")
            content = _flatten_content(raw_content)
            tool_calls: list[ToolCall] = [
                ToolCall(
                    tool_call_id=request.get("toolCallId", ""),
                    function_name=request.get("name", ""),
                    arguments=_normalize_tool_arguments(request.get("arguments")),
                )
                for request in data.get("toolRequests") or []
            ]
            output_tokens = data.get("outputTokens") or 0
            state["out"] += output_tokens

            # Copilot carries the turn's reasoning inline on ``reasoningText``
            # (grounded in real session output). Map it to ATIF's first-class
            # ``reasoning_content`` rather than burying it in ``extra``. The
            # opaque/streamed variants (``reasoningText`` duplicates the
            # standalone ``assistant.reasoning`` event; ``reasoningOpaque`` is
            # an encrypted echo) are excluded from ``extra`` here.
            reasoning_text = data.get("reasoningText") or None

            mapped_keys = {
                "content",
                "toolRequests",
                "outputTokens",
                "model",
                "reasoningText",
                "reasoningOpaque",
            }
            extra = {k: v for k, v in data.items() if k not in mapped_keys} or None

            # Skip only a genuinely empty turn, not one whose content flattened
            # to "" (its tokens are already summed above).
            if not raw_content and not tool_calls and not reasoning_text and not extra:
                return

            step = Step(
                step_id=state["step_id"],
                timestamp=timestamp,
                source="agent",
                message=content,
                model_name=data.get("model") or None,
                reasoning_content=reasoning_text,
                tool_calls=tool_calls or None,
                metrics=(Metrics(completion_tokens=output_tokens) if output_tokens else None),
                extra=extra,
            )
            steps.append(step)
            state["step_id"] += 1
            for tool_call in tool_calls:
                if tool_call.tool_call_id:
                    call_id_map[tool_call.tool_call_id] = step

        # --- assistant.reasoning (session-event schema) ---
        # A standalone reasoning event that duplicates the ``reasoningText``
        # already captured on the corresponding ``assistant.message``. Skip it
        # to avoid emitting the reasoning twice (it is tallied like other
        # lifecycle events so a shape change stays visible).
        elif event_type == "assistant.reasoning":
            skipped_event_types["assistant.reasoning"] = skipped_event_types.get("assistant.reasoning", 0) + 1

        # --- user.message (session-event schema) ---
        elif event_type == "user.message":
            data = event["data"]
            content = _flatten_content(data.get("content"))
            extra = {k: v for k, v in data.items() if k != "content"} or None
            if content or extra:
                steps.append(
                    Step(
                        step_id=state["step_id"],
                        timestamp=timestamp,
                        source="user",
                        message=content,
                        extra=extra,
                    )
                )
                state["step_id"] += 1

        # --- tool.execution_complete (session-event schema) ---
        elif event_type == "tool.execution_complete":
            data = event["data"]
            content = _stringify_tool_result(data.get("result"))
            error = data.get("error")
            if error is not None:
                error_text = _stringify_tool_result(error)
                content = error_text if not content else f"{content}\n{error_text}"
            result_extra = {k: v for k, v in data.items() if k not in ("result", "error", "toolCallId")} or None
            state["step_id"] = _record_tool_result(
                call_id_map,
                steps,
                state["step_id"],
                call_id=data.get("toolCallId"),
                content=content,
                timestamp=timestamp,
                extra=result_extra,
            )

        # --- result (session-event schema) ---
        elif event_type == "result":
            state["result"] = {k: v for k, v in event.items() if k not in ("type", "id", "parentId")} or None

        # Streaming/lifecycle events carry nothing the consolidated events don't
        # already provide. Tally unhandled types so a new type stays visible.
        else:
            key = event_type if isinstance(event_type, str) else "<missing>"
            skipped_event_types[key] = skipped_event_types.get(key, 0) + 1

    for event in raw_events:
        if not isinstance(event, dict):
            skipped_event_types["<non-object>"] = skipped_event_types.get("<non-object>", 0) + 1
            continue
        try:
            _handle_event(event)
        except Exception as exc:
            failed_events += 1
            logger.debug(
                "Salvaging a Copilot CLI event (type=%r) that failed to convert: %s",
                event.get("type"),
                exc,
                exc_info=True,
            )
            state["step_id"] = _record_unparsed_event(steps, state["step_id"], event, exc)

    if failed_events:
        logger.debug("Copilot CLI: salvaged %d event(s) that failed to convert", failed_events)
    if skipped_event_types:
        logger.debug(
            "Copilot CLI: did not map %d event(s) of types %s",
            sum(skipped_event_types.values()),
            skipped_event_types,
        )

    if not steps:
        return None

    default_model: str | None = None
    for step in steps:
        if step.source == "agent" and step.model_name:
            default_model = step.model_name
            break

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id="copilot-cli",
        agent=Agent(
            name=AGENT_NAME,
            version="unknown",
            model_name=default_model,
        ),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=state["in"] or None,
            total_completion_tokens=state["out"] or None,
            total_steps=len(steps),
            extra={"copilot_result": state["result"]} if state["result"] else None,
        ),
    )


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def _find_jsonl(run_dir: Path) -> Path | None:
    """Locate the Copilot CLI JSONL output within a run directory."""
    candidate = run_dir / _JSONL_FILENAME
    if candidate.exists():
        return candidate
    return None


def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert a Copilot CLI run directory into a validated ATIF ``Trajectory``.

    Args:
        run_dir: Canonical run directory
            (``results/<batch>/copilot/<problem_id>/run_<n>/``) containing
            ``copilot-cli.jsonl``.
        sregym_meta: Optional SREGym metadata to attach under ``extra.sregym``.
            Assembly of the full payload (application mapping, boundary
            detection) is done by ``convert.py``; this adapter stores it verbatim.

    Returns:
        A validated ``Trajectory``, or ``None`` if no convertible JSONL exists.
    """
    run_dir = Path(run_dir)
    jsonl_path = _find_jsonl(run_dir)
    if jsonl_path is None:
        logger.debug("No Copilot CLI JSONL (%s) found in %s", _JSONL_FILENAME, run_dir)
        return None

    raw_events = _read_copilot_cli_jsonl(jsonl_path)
    trajectory = _convert_events(raw_events)
    if trajectory is None:
        return None

    if sregym_meta:
        trajectory.extra = {"sregym": dict(sregym_meta)}

    return trajectory
