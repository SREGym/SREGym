"""OpenCode -> ATIF v1.7 adapter.

A clean port of Harbor's ``OpenCode._convert_events_to_trajectory`` into
standalone, pure functions with no dependency on ``harbor`` or
``BaseInstalledAgent``.

Reads the **session JSON** exported by ``opencode export`` (captured to
``sessions/YYYY/MM/DD/session-<id>.json``). The session JSON carries agent
metadata (model, version), the user turn, and authoritative aggregate token
counts. If no session was exported (``export_session()`` failed), the adapter
returns ``None`` — the client's export is the reliability point, same as
codex's session copy.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..atif import (
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

AGENT_NAME = "opencode"


# --------------------------------------------------------------------------- #
# Helpers (ported from Harbor's static methods)
# --------------------------------------------------------------------------- #
def _millis_to_iso(timestamp_ms: int | float | None) -> str | None:
    """Convert a millisecond Unix timestamp to ISO 8601 string."""
    if timestamp_ms is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def _metrics_from_finish(finish: dict[str, Any]) -> tuple[Metrics | None, dict[str, int | None]]:
    """Build per-step Metrics + a raw-tokens dict from a step-finish part."""
    tokens = finish.get("tokens", {})
    cost = finish.get("cost", 0) or 0
    input_tok = tokens.get("input", 0) or 0
    output_tok = tokens.get("output", 0) or 0
    reasoning_tok = tokens.get("reasoning", 0) or 0
    cache = tokens.get("cache", {})
    cache_read = cache.get("read", 0) or 0
    cache_write = cache.get("write", 0) or 0

    raw = {
        "input": input_tok,
        "output": output_tok,
        "reasoning": reasoning_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost": cost,
    }

    metrics: Metrics | None = None
    if input_tok or output_tok or cache_read:
        metrics = Metrics(
            prompt_tokens=input_tok + cache_read,
            completion_tokens=output_tok,
            cached_tokens=cache_read if cache_read else None,
            cost_usd=cost if cost else None,
            extra={
                k: v
                for k, v in {
                    "reasoning_tokens": reasoning_tok,
                    "cache_write_tokens": cache_write,
                }.items()
                if v
            }
            or None,
        )
    return metrics, raw


def _build_step_from_parts(
    parts: list[dict[str, Any]],
    finish: dict[str, Any],
    step_id: int,
    timestamp: str | None,
    model_name: str | None,
) -> tuple[Step, dict[str, int | None]]:
    """Build an agent Step from a list of OpenCode parts + a step-finish part."""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls_list: list[ToolCall] = []
    observation_results: list[ObservationResult] = []

    for part in parts:
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if text:
                text_parts.append(text)
        elif ptype == "reasoning":
            reasoning = part.get("text", "")
            if reasoning:
                reasoning_parts.append(reasoning)
        elif ptype == "tool":
            state = part.get("state", {})
            tool_name = part.get("tool", "")
            tool_input = state.get("input", {})
            tool_output = state.get("output")
            call_id = part.get("callID", part.get("id", ""))
            if not isinstance(tool_input, dict):
                tool_input = {"value": tool_input} if tool_input else {}
            tool_calls_list.append(ToolCall(tool_call_id=call_id, function_name=tool_name, arguments=tool_input))
            if tool_output is not None:
                observation_results.append(ObservationResult(source_call_id=call_id or None, content=str(tool_output)))

    metrics, raw = _metrics_from_finish(finish)
    message_text = "\n".join(text_parts) if text_parts else ""
    observation = Observation(results=observation_results) if observation_results else None

    step_kwargs: dict[str, Any] = {
        "step_id": step_id,
        "timestamp": timestamp,
        "source": "agent",
        "message": message_text,
        "llm_call_count": 1,
    }
    if model_name:
        step_kwargs["model_name"] = model_name
    if reasoning_parts:
        step_kwargs["reasoning_content"] = "\n\n".join(reasoning_parts)
    if tool_calls_list:
        step_kwargs["tool_calls"] = tool_calls_list
    if observation:
        step_kwargs["observation"] = observation
    if metrics:
        step_kwargs["metrics"] = metrics
    return Step(**step_kwargs), raw


# --------------------------------------------------------------------------- #
# Session JSON converter (primary)
# --------------------------------------------------------------------------- #
def _convert_from_session(session_path: Path) -> Trajectory | None:
    """Convert an ``opencode export`` session JSON into an ATIF trajectory."""
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Skipping unreadable OpenCode session %s: %s", session_path, exc)
        return None
    if not isinstance(data, dict) or "messages" not in data:
        return None

    info = data.get("info", {}) if isinstance(data.get("info"), dict) else {}
    session_id = info.get("id") or session_path.stem.removeprefix("session-")
    version = info.get("version") or "unknown"
    model_info = info.get("model", {}) if isinstance(info.get("model"), dict) else {}
    model_name = model_info.get("id") or model_info.get("modelID")

    # Authoritative aggregate tokens (matches results JSON).
    info_tokens = info.get("tokens", {}) if isinstance(info.get("tokens"), dict) else {}
    agg_input = info_tokens.get("input", 0) or 0
    agg_output = info_tokens.get("output", 0) or 0
    agg_reasoning = info_tokens.get("reasoning", 0) or 0
    agg_cache = info_tokens.get("cache", {}) if isinstance(info_tokens.get("cache"), dict) else {}
    agg_cache_read = agg_cache.get("read", 0) or 0
    agg_cache_write = agg_cache.get("write", 0) or 0
    agg_cost = info.get("cost", 0) or 0

    steps: list[Step] = []
    step_id = 1

    for message in data["messages"]:
        if not isinstance(message, dict):
            continue
        msg_info = message.get("info", {}) if isinstance(message.get("info"), dict) else {}
        role = msg_info.get("role")
        parts = message.get("parts", []) if isinstance(message.get("parts"), list) else []
        time_info = msg_info.get("time", {}) if isinstance(msg_info.get("time"), dict) else {}
        timestamp = _millis_to_iso(time_info.get("created"))

        if role == "user":
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
            user_text = "\n".join(t for t in texts if t)
            if user_text:
                steps.append(Step(step_id=step_id, timestamp=timestamp, source="user", message=user_text))
                step_id += 1
            continue

        if role != "assistant":
            continue

        # One assistant message = one agent step. Collect non-step-finish
        # parts as content; the step-finish part carries the metrics.
        content_parts = [p for p in parts if isinstance(p, dict) and p.get("type") != "step-finish"]
        finish_part = next((p for p in parts if isinstance(p, dict) and p.get("type") == "step-finish"), {})
        step, _raw = _build_step_from_parts(content_parts, finish_part, step_id, timestamp, model_name)
        steps.append(step)
        step_id += 1

    if not steps:
        return None

    final_metrics = FinalMetrics(
        total_prompt_tokens=(agg_input + agg_cache_read) or None,
        total_completion_tokens=agg_output or None,
        total_cached_tokens=agg_cache_read or None,
        total_cost_usd=agg_cost if agg_cost else None,
        total_steps=len(steps),
        extra={
            k: v
            for k, v in {
                "input_tokens": agg_input or None,
                "reasoning_tokens": agg_reasoning or None,
                "cache_write_tokens": agg_cache_write or None,
            }.items()
            if v
        }
        or None,
    )

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        agent=Agent(name=AGENT_NAME, version=version, model_name=model_name),
        steps=steps,
        final_metrics=final_metrics,
    )


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def convert_file(session_file: Path | str) -> Trajectory | None:
    """Convert one exported OpenCode session JSON file to ATIF."""
    return _convert_from_session(Path(session_file))
