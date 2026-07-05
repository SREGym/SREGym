"""Codex CLI -> ATIF v1.7 adapter.

A clean port of Harbor's ``Codex._convert_events_to_trajectory`` and its
helpers (upstream commit ``fd1a8ea``; see ``sregym/traces/atif/UPSTREAM.md``)
into standalone, pure functions with no dependency on ``harbor`` or
``BaseInstalledAgent``.

The conversion reads Codex **session JSONL** files (captured under
``$CODEX_HOME/sessions/`` and copied into ``<run_dir>/sessions/`` by the codex
client's ``finally`` block) and produces one validated ATIF ``Trajectory``
covering the whole session.

Key behavior ported from Harbor:

- **api-call grouping**: Codex emits several ``response_item`` events
  (``reasoning`` / ``message`` / ``function_call`` / ``function_call_output``)
  that all belong to the same model request. They are bundled into a single
  ATIF step by an inferred ``api_call_id`` counter, where each ``token_count``
  ``event_msg`` closes one API call. Without this, steps and tool calls would
  fragment unnaturally.
- **per-call metrics** come from the ``token_count`` event's
  ``info.last_token_usage`` block; ``total_token_usage`` provides the run
  aggregate for ``FinalMetrics``.
- **cost** is not reported by Codex CLI, so ``total_cost_usd`` is ``None``
  (no LiteLLM pricing-table dependency).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from sregym.traces.adapters._common import _load_jsonl
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

AGENT_NAME = "codex"


# --------------------------------------------------------------------------- #
# Session-directory discovery
# --------------------------------------------------------------------------- #
def _get_session_dir(run_dir: Path) -> Path | None:
    """Identify the Codex session directory containing the primary JSONL log.

    Ported from Harbor's ``_get_session_dir``: looks under ``<run_dir>/sessions/``
    for the deepest directory containing session files.
    """
    sessions_root = run_dir / "sessions"
    if not sessions_root.exists():
        return None

    session_dirs = [d for d in sessions_root.rglob("*") if d.is_dir()]
    if not session_dirs:
        return None

    max_depth = max(len(d.parts) for d in session_dirs)
    session_dirs = [d for d in session_dirs if len(d.parts) == max_depth]
    if not session_dirs:
        return None

    if len(session_dirs) != 1:
        logger.debug(
            "Expected exactly 1 Codex session, found %d in %s",
            len(session_dirs),
            run_dir,
        )
        return None
    return session_dirs[0]


# --------------------------------------------------------------------------- #
# Content extraction helpers (ported verbatim from Harbor's static methods)
# --------------------------------------------------------------------------- #
def _extract_message_text(content: list[Any]) -> str:
    """Extract joined text from Codex content blocks."""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _parse_output_blob(raw: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Extract textual output and metadata from Codex tool outputs."""
    if raw is None:
        return None, None

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw, None
    else:
        parsed = raw

    if isinstance(parsed, dict):
        output = parsed.get("output")
        if output is None and parsed:
            # Dumping remaining structure if output missing.
            output = json.dumps(parsed, ensure_ascii=False)
        metadata = parsed.get("metadata")
        return output, metadata if isinstance(metadata, dict) else None

    return str(parsed), None


def _metrics_from_token_count_payload(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a per-step metrics dict from a ``token_count`` event payload."""
    info = payload.get("info")
    if not isinstance(info, dict):
        return None

    last_usage = info.get("last_token_usage")
    if not isinstance(last_usage, dict):
        return None

    prompt_tokens = last_usage.get("input_tokens")
    completion_tokens = last_usage.get("output_tokens")
    cached_tokens = last_usage.get("cached_input_tokens")
    reasoning_tokens = last_usage.get("reasoning_output_tokens")
    total_tokens = last_usage.get("total_tokens")

    return {
        "prompt_tokens": prompt_tokens if prompt_tokens else None,
        "completion_tokens": completion_tokens or None,
        "cached_tokens": cached_tokens or None,
        "extra": {
            "reasoning_output_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
        },
    }


# --------------------------------------------------------------------------- #
# Normalized-event -> Step
# --------------------------------------------------------------------------- #
def _convert_event_to_step(
    event: dict[str, Any],
    step_id: int,
    default_model_name: str | None,
) -> Step:
    """Convert a normalized Codex event dictionary into an ATIF step.

    Ported from Harbor's ``_convert_event_to_step``; ``self.model_name`` is
    replaced by the ``default_model_name`` parameter.
    """
    kind = event.get("kind")
    timestamp = event.get("timestamp")

    if kind == "message":
        role = event.get("role", "user")
        text = event.get("text", "")
        reasoning = event.get("reasoning")
        source: Literal["system", "user", "agent"]
        if role == "assistant":
            source = "agent"
        elif role == "user":
            source = "user"
        else:
            source = "system"

        extra = event.get("extra")

        return Step(
            step_id=step_id,
            timestamp=timestamp,
            source=source,
            message=text,
            reasoning_content=reasoning if source == "agent" and reasoning else None,
            model_name=default_model_name if source == "agent" and default_model_name else None,
            llm_call_count=1 if source == "agent" else None,
            extra=extra if extra else None,
        )

    if kind == "tool_call":
        call_id = event.get("call_id", "")
        tool_name = event.get("tool_name", "")
        reasoning = event.get("reasoning")
        arguments = event.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}

        tool_call = ToolCall(
            tool_call_id=call_id,
            function_name=tool_name,
            arguments=arguments,
        )

        observation: Observation | None = None
        output_text = event.get("output")
        if output_text is not None:
            observation = Observation(
                results=[
                    ObservationResult(
                        source_call_id=call_id or None,
                        content=output_text,
                    )
                ]
            )

        metrics_payload = event.get("metrics")
        metrics: Metrics | None = None
        if isinstance(metrics_payload, dict):
            metrics = Metrics(**metrics_payload)

        extra: dict[str, Any] | None = None
        metadata = event.get("metadata")
        if metadata:
            extra = {"tool_metadata": metadata}
        raw_arguments = event.get("raw_arguments")
        if raw_arguments:
            extra = extra or {}
            extra["raw_arguments"] = raw_arguments
        status = event.get("status")
        if status:
            extra = extra or {}
            extra["status"] = status
        api_call_id = event.get("api_call_id")
        if api_call_id:
            extra = extra or {}
            extra["api_call_id"] = api_call_id
        codex_turn_id = event.get("codex_turn_id")
        if codex_turn_id:
            extra = extra or {}
            extra["codex_turn_id"] = codex_turn_id

        message = event.get("message") or ""

        return Step(
            step_id=step_id,
            timestamp=timestamp,
            source="agent",
            message=message,
            tool_calls=[tool_call],
            observation=observation,
            model_name=default_model_name,
            reasoning_content=reasoning if reasoning else None,
            metrics=metrics,
            llm_call_count=1,
            extra=extra,
        )

    if kind == "bundled":
        text = event.get("text", "")
        reasoning = event.get("reasoning")

        tool_calls: list[ToolCall] = []
        observation_results: list[ObservationResult] = []
        for tc in event.get("tool_calls", []):
            call_id = tc.get("call_id", "")
            arguments = tc.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}

            tool_calls.append(
                ToolCall(
                    tool_call_id=call_id,
                    function_name=tc.get("tool_name", ""),
                    arguments=arguments,
                )
            )
            observation_results.append(
                ObservationResult(
                    source_call_id=call_id or None,
                    content=tc.get("output"),
                )
            )

        extra: dict[str, Any] | None = None
        api_call_id = event.get("api_call_id")
        if api_call_id:
            extra = {"api_call_id": api_call_id}
        codex_turn_id = event.get("codex_turn_id")
        if codex_turn_id:
            extra = extra or {}
            extra["codex_turn_id"] = codex_turn_id

        tool_details: dict[str, Any] = {}
        for tc in event.get("tool_calls", []):
            call_id = tc.get("call_id", "")
            details: dict[str, Any] = {}
            for source_key, target_key in (
                ("metadata", "metadata"),
                ("raw_arguments", "raw_arguments"),
                ("status", "status"),
            ):
                value = tc.get(source_key)
                if value:
                    details[target_key] = value
            if details:
                tool_details[call_id] = details
        if tool_details:
            extra = extra or {}
            extra["tool_call_details"] = tool_details

        observation = Observation(results=observation_results) if observation_results else None

        return Step(
            step_id=step_id,
            timestamp=event.get("timestamp"),
            source="agent",
            message=text,
            model_name=default_model_name,
            reasoning_content=reasoning if reasoning else None,
            tool_calls=tool_calls or None,
            observation=observation,
            metrics=Metrics(**event["metrics"]) if event.get("metrics") else None,
            llm_call_count=1,
            extra=extra,
        )

    raise ValueError(f"Unsupported event kind '{kind}'")


def _group_events_by_api_call_id(
    normalized_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge assistant events from the same Codex model request into one step.

    Ported verbatim from Harbor's ``_group_events_by_api_call_id``. Events that
    share an inferred ``api_call_id`` are bundled: their messages are joined,
    tool calls collected (sorted by ``tool_order``), and metrics attached.
    """
    result: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    group_order: list[str] = []

    def flush() -> None:
        for group_id in group_order:
            group = groups.pop(group_id, None)
            if group is None:
                continue
            group["tool_calls"].sort(key=lambda tc: tc.get("tool_order", 0))
            message_parts = [part for part in group.pop("message_parts") if isinstance(part, str) and part]
            group["text"] = "\n\n".join(message_parts)
            result.append(group)
        group_order.clear()

    for event in normalized_events:
        api_call_id = event.get("api_call_id")
        kind = event.get("kind")
        role = event.get("role")

        if kind == "message" and role != "assistant":
            flush()
            result.append(event)
            continue

        if not isinstance(api_call_id, str):
            flush()
            result.append(event)
            continue

        if api_call_id not in groups:
            groups[api_call_id] = {
                "kind": "bundled",
                "api_call_id": api_call_id,
                "codex_turn_id": event.get("codex_turn_id"),
                "timestamp": event.get("timestamp"),
                "message_parts": [],
                "reasoning": None,
                "tool_calls": [],
                "metrics": event.get("metrics"),
            }
            group_order.append(api_call_id)

        group = groups[api_call_id]
        if kind == "message":
            text = event.get("text")
            if isinstance(text, str) and text:
                group["message_parts"].append(text)
            if event.get("reasoning"):
                group["reasoning"] = event["reasoning"]
            if event.get("timestamp"):
                group["timestamp"] = event["timestamp"]
        elif kind == "tool_call":
            group["tool_calls"].append(event)
            if not group["reasoning"] and event.get("reasoning"):
                group["reasoning"] = event["reasoning"]
            if not group.get("metrics") and event.get("metrics"):
                group["metrics"] = event["metrics"]

    flush()
    return result


# --------------------------------------------------------------------------- #
# Final metrics extraction
# --------------------------------------------------------------------------- #
def _extract_final_metrics(raw_events: list[dict[str, Any]], total_steps: int) -> FinalMetrics | None:
    """Extract aggregate ``FinalMetrics`` from the last ``token_count`` event.

    Ported from Harbor; cost is left as ``None`` (Codex CLI does not report
    cost and we do not use LiteLLM pricing).
    """
    for event in reversed(raw_events):
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue

        info = payload.get("info")
        if not isinstance(info, dict):
            continue

        total_usage = info.get("total_token_usage")
        if not isinstance(total_usage, dict):
            continue

        prompt_tokens = total_usage.get("input_tokens")
        completion_tokens = total_usage.get("output_tokens")
        reasoning_tokens = total_usage.get("reasoning_output_tokens")
        cached_tokens = total_usage.get("cached_input_tokens")
        overall_tokens = total_usage.get("total_tokens")

        # Codex CLI does not include cost in token_count events; leave as None.
        total_cost_usd = info.get("total_cost")
        if total_cost_usd is None:
            total_cost_usd = info.get("cost_usd")

        final_extra: dict[str, Any] | None = {
            "reasoning_output_tokens": reasoning_tokens,
            "total_tokens": overall_tokens,
            "last_token_usage": info.get("last_token_usage"),
        }

        return FinalMetrics(
            total_prompt_tokens=prompt_tokens if prompt_tokens else None,
            total_completion_tokens=completion_tokens or None,
            total_cached_tokens=cached_tokens or None,
            total_cost_usd=total_cost_usd,
            total_steps=total_steps,
            extra=final_extra,
        )
    return None


# --------------------------------------------------------------------------- #
# Main conversion
# --------------------------------------------------------------------------- #
def _convert_session(session_dir: Path) -> Trajectory | None:
    """Convert Codex session JSONL events into an ATIF trajectory."""
    session_files = list(session_dir.glob("*.jsonl"))
    if not session_files:
        logger.debug("No Codex session files found in %s", session_dir)
        return None

    session_file = session_files[0]
    raw_events = _load_jsonl(session_file)
    if not raw_events:
        return None

    # Session metadata.
    session_meta = next((e for e in raw_events if e.get("type") == "session_meta"), None)
    session_id = (
        session_meta.get("payload", {}).get("id")
        if session_meta and isinstance(session_meta, dict)
        else session_dir.name
    )

    agent_version = "unknown"
    agent_extra: dict[str, Any] | None = None
    default_model_name: str | None = None

    if session_meta:
        payload = session_meta.get("payload", {})
        agent_version = payload.get("cli_version") or agent_version
        extra: dict[str, Any] = {}
        for key in ("originator", "cwd", "git", "instructions"):
            value = payload.get(key)
            if value is not None:
                extra[key] = value
        agent_extra = extra or None

    for event in raw_events:
        if event.get("type") == "turn_context":
            model_name = event.get("payload", {}).get("model")
            if isinstance(model_name, str):
                default_model_name = model_name
                break

    # Normalize events to a structure suitable for conversion into Steps.
    normalized_events: list[dict[str, Any]] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    pending_reasoning: str | None = None
    codex_turn_id: str | None = None
    api_call_index = 1
    current_api_call_id = f"api_call_{api_call_index}"
    api_call_metrics: dict[str, dict[str, Any]] = {}
    saw_model_output_in_api_call = False
    tool_order_counter = 0

    def record_model_output() -> None:
        nonlocal saw_model_output_in_api_call
        saw_model_output_in_api_call = True

    def finish_api_call(token_count_payload: dict[str, Any]) -> None:
        nonlocal api_call_index, current_api_call_id, saw_model_output_in_api_call
        nonlocal tool_order_counter

        if not saw_model_output_in_api_call:
            return

        metrics = _metrics_from_token_count_payload(token_count_payload)
        if metrics:
            api_call_metrics[current_api_call_id] = metrics

        api_call_index += 1
        current_api_call_id = f"api_call_{api_call_index}"
        saw_model_output_in_api_call = False
        tool_order_counter = 0

    for event in raw_events:
        etype = event.get("type")
        payload = event.get("payload", {})
        timestamp = event.get("timestamp")

        if etype == "event_msg" and isinstance(payload, dict):
            event_type = payload.get("type")
            if event_type in {"task_started", "turn_started"}:
                turn_id = payload.get("turn_id")
                codex_turn_id = turn_id if isinstance(turn_id, str) else None
            elif event_type in {"task_complete", "turn_complete", "turn_aborted"}:
                codex_turn_id = None
            elif event_type == "token_count":
                # A token_count event closes one model API call.
                finish_api_call(payload)
            continue

        if etype == "turn_context":
            turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
            if isinstance(turn_id, str) and codex_turn_id is None:
                codex_turn_id = turn_id
            continue

        if etype != "response_item":
            continue

        payload_type = payload.get("type")
        if payload_type == "reasoning":
            summary = payload.get("summary")
            if isinstance(summary, list) and summary:
                reasoning_parts: list[str] = []
                for item in summary:
                    if isinstance(item, str):
                        reasoning_parts.append(item)
                    elif isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str):
                            reasoning_parts.append(text)
                pending_reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
            else:
                pending_reasoning = None
            continue

        if payload_type == "message":
            content = payload.get("content", [])
            text = _extract_message_text(content) if isinstance(content, list) else ""
            normalized_events.append(
                {
                    "kind": "message",
                    "api_call_id": current_api_call_id,
                    "codex_turn_id": codex_turn_id,
                    "timestamp": timestamp,
                    "role": payload.get("role", "user"),
                    "text": text,
                    "reasoning": pending_reasoning if payload.get("role") == "assistant" else None,
                }
            )
            if payload.get("role") == "assistant":
                record_model_output()
            pending_reasoning = None
            continue

        if payload_type == "web_search_call":
            action = payload.get("action") or {}
            action_type = action.get("type", "")
            arguments: dict[str, Any] = {"action_type": action_type}
            if "query" in action:
                arguments["query"] = action["query"]
            if "queries" in action:
                arguments["queries"] = action["queries"]
            if "url" in action:
                arguments["url"] = action["url"]

            normalized_events.append(
                {
                    "kind": "tool_call",
                    "api_call_id": current_api_call_id,
                    "codex_turn_id": codex_turn_id,
                    "tool_order": tool_order_counter,
                    "timestamp": timestamp,
                    "call_id": "",
                    "tool_name": "web_search_call",
                    "arguments": arguments,
                    "raw_arguments": None,
                    "reasoning": pending_reasoning,
                    "status": payload.get("status"),
                    "message": None,
                }
            )
            tool_order_counter += 1
            record_model_output()
            pending_reasoning = None
            continue

        if payload_type in {"function_call", "custom_tool_call"}:
            call_id = payload.get("call_id")
            if not call_id:
                continue

            raw_args_key = "arguments" if payload_type == "function_call" else "input"
            raw_arguments = payload.get(raw_args_key)
            try:
                parsed_args = json.loads(raw_arguments)
            except (json.JSONDecodeError, TypeError):
                if isinstance(raw_arguments, str):
                    parsed_args = {"input": raw_arguments}
                elif raw_arguments is None:
                    parsed_args = {}
                else:
                    parsed_args = {"value": raw_arguments}

            pending_calls[call_id] = {
                "kind": "tool_call",
                "api_call_id": current_api_call_id,
                "codex_turn_id": codex_turn_id,
                "tool_order": tool_order_counter,
                "timestamp": timestamp,
                "call_id": call_id,
                "tool_name": payload.get("name") or "",
                "arguments": parsed_args,
                "raw_arguments": raw_arguments,
                "reasoning": pending_reasoning,
                "status": payload.get("status"),
                "message": None,
            }
            tool_order_counter += 1
            record_model_output()
            pending_reasoning = None
            continue

        if payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = payload.get("call_id")
            output_text, metadata = _parse_output_blob(payload.get("output"))

            call_info = pending_calls.pop(call_id, None) if call_id else None

            if call_info is None:
                call_info = {
                    "kind": "tool_call",
                    "api_call_id": current_api_call_id,
                    "codex_turn_id": codex_turn_id,
                    "tool_order": tool_order_counter,
                    "timestamp": timestamp,
                    "call_id": call_id or "",
                    "tool_name": payload.get("name", "") or "",
                    "arguments": {},
                    "raw_arguments": None,
                    "reasoning": pending_reasoning,
                    "status": None,
                    "message": None,
                }
                tool_order_counter += 1

            call_info["output"] = output_text
            call_info["metadata"] = metadata
            call_info["timestamp"] = call_info.get("timestamp") or timestamp
            normalized_events.append(call_info)
            pending_reasoning = None
            continue

    # Attach per-call metrics to normalized events.
    for norm_event in normalized_events:
        event_api_call_id = norm_event.get("api_call_id")
        if isinstance(event_api_call_id, str) and event_api_call_id in api_call_metrics:
            norm_event["metrics"] = api_call_metrics[event_api_call_id]

    grouped_events = _group_events_by_api_call_id(normalized_events)

    steps: list[Step] = []
    for idx, norm_event in enumerate(grouped_events, start=1):
        try:
            step = _convert_event_to_step(norm_event, idx, default_model_name)
        except ValueError as exc:
            logger.debug("Skipping event during step conversion: %s", exc)
            continue

        # Provide default model name if not set for agent steps.
        if step.source == "agent" and not step.model_name and default_model_name:
            step.model_name = default_model_name

        steps.append(step)

    if not steps:
        logger.debug("No valid steps produced from Codex session")
        return None

    final_metrics = _extract_final_metrics(raw_events, len(steps))

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        agent=Agent(
            name=AGENT_NAME,
            version=agent_version,
            model_name=default_model_name,
            extra=agent_extra,
        ),
        steps=steps,
        final_metrics=final_metrics,
    )


def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert a Codex run directory into a validated ATIF ``Trajectory``.

    Args:
        run_dir: Canonical run directory
            (``results/<batch>/codex/<problem_id>/run_<n>/``).
        sregym_meta: Optional SREGym metadata to attach under ``extra.sregym``.
            Assembly of the full ``extra.sregym`` payload (application mapping,
            boundary detection) is the responsibility of ``convert.py``; this
            adapter only stores whatever dict it is handed.

    Returns:
        A validated ``Trajectory``, or ``None`` if no convertible session exists.
    """
    run_dir = Path(run_dir)
    session_dir = _get_session_dir(run_dir)
    if not session_dir:
        logger.debug("No Codex session directory found in %s", run_dir)
        return None

    trajectory = _convert_session(session_dir)
    if trajectory is None:
        return None

    if sregym_meta:
        trajectory.extra = {"sregym": dict(sregym_meta)}

    return trajectory
