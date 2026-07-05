"""Gemini CLI -> ATIF v1.7 adapter.

A clean port of Harbor's ``GeminiCli._convert_gemini_to_atif`` and
``_load_gemini_session`` (see ``sregym/traces/atif/UPSTREAM.md``) into
standalone, pure functions with no dependency on ``harbor`` or
``BaseInstalledAgent``.

The conversion reads the Gemini CLI **session JSON** that the geminicli client
archives into ``<run_dir>/sessions/YYYY/MM/DD/session-<id>.json``.

Two on-disk shapes are handled (``_load_gemini_session`` normalizes both to the
legacy ``{sessionId, messages:[...]}`` form):

- **Legacy single-JSON** (what SREGym currently archives): one JSON object with
  a ``messages`` array.
- **JSONL** (Gemini CLI v0.40+): one record per line with ``message_update`` /
  ``$rewindTo`` / ``$set`` records that are replayed into messages.

Message schema:

    {"type": "user", "content": "...", "timestamp": "..."}
    {"type": "gemini", "content": "...", "model": "gemini-2.5-pro",
     "thoughts": [{"subject": "...", "description": "..."}],
     "toolCalls": [{"id": "...", "name": "...", "args": {...},
        "result": [{"functionResponse": {"response": {"output": "..."}}}]}],
     "tokens": {"input": N, "output": N, "cached": N, "thoughts": N, "tool": N}}

Deliberate simplifications vs. Harbor (matching our other adapters):

- No multimodal image extraction (Harbor writes base64 images to an ``images/``
  dir). SRE diagnosis runs are text/tool-output only; image parts fold to a
  ``[image]`` text placeholder to keep the adapter pure/side-effect-free.
- No cost computation (Harbor backs cost out of LiteLLM pricing). ``total_cost_usd``
  is left ``None`` (session has no cost field), same as codex.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sregym.traces.adapters._common import _stringify
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

AGENT_NAME = "gemini"


# --------------------------------------------------------------------------- #
# Session-file discovery
# --------------------------------------------------------------------------- #
def _find_session_file(run_dir: Path) -> Path | None:
    """Find the archived Gemini session JSON under ``<run_dir>/sessions/``.

    The geminicli client archives to ``sessions/YYYY/MM/DD/session-<id>.json``.
    Returns the most-recently-modified match, or None.
    """
    sessions_root = run_dir / "sessions"
    if not sessions_root.exists():
        return None
    candidates = list(sessions_root.rglob("session-*.json")) + list(sessions_root.rglob("session-*.jsonl"))
    if not candidates:
        # Fallback: any json/jsonl under sessions/.
        candidates = list(sessions_root.rglob("*.json")) + list(sessions_root.rglob("*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# --------------------------------------------------------------------------- #
# Session loading (legacy JSON + JSONL) — ported from Harbor
# --------------------------------------------------------------------------- #
def _merge_message_update(message: dict[str, Any], update: dict[str, Any]) -> None:
    """Apply a Gemini JSONL ``message_update`` record to a message record."""
    for key, value in update.items():
        if key in {"type", "id"}:
            continue
        current = message.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            current.update(value)
        else:
            message[key] = value


def _load_gemini_session(path: Path) -> dict[str, Any] | None:
    """Load a Gemini session file, normalizing JSONL to the legacy shape."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("Error reading Gemini session %s: %s", path, exc)
        return None
    if not text.strip():
        return None

    # Legacy single-JSON blob.
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "messages" in data:
            return data
    except json.JSONDecodeError:
        pass

    # JSONL: replay message / message_update / $rewindTo / $set records.
    metadata: dict[str, Any] = {}
    message_ids: list[str] = []
    messages_by_id: dict[str, dict[str, Any]] = {}
    pending_updates: dict[str, list[dict[str, Any]]] = {}

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        if "$rewindTo" in record:
            rewind_id = record["$rewindTo"]
            if rewind_id in message_ids:
                idx = message_ids.index(rewind_id)
                for removed in message_ids[idx:]:
                    messages_by_id.pop(removed, None)
                    pending_updates.pop(removed, None)
                del message_ids[idx:]
            else:
                message_ids.clear()
                messages_by_id.clear()
                pending_updates.clear()
        elif "$set" in record and isinstance(record["$set"], dict):
            metadata.update(record["$set"])
        elif record.get("type") in {"user", "gemini"}:
            mid = record.get("id")
            if isinstance(mid, str) and mid in messages_by_id:
                message = messages_by_id[mid]
                message.update(record)
            else:
                message = record
                if isinstance(mid, str):
                    message_ids.append(mid)
                    messages_by_id[mid] = message
            if isinstance(mid, str):
                for update in pending_updates.pop(mid, []):
                    _merge_message_update(message, update)
        elif record.get("type") == "message_update":
            mid = record.get("id")
            if isinstance(mid, str) and mid in messages_by_id:
                _merge_message_update(messages_by_id[mid], record)
            elif isinstance(mid, str):
                pending_updates.setdefault(mid, []).append(record)
        elif "sessionId" in record:
            for k, v in record.items():
                if k != "messages":
                    metadata[k] = v

    if not message_ids and not metadata:
        return None

    result: dict[str, Any] = {
        "sessionId": metadata.get("sessionId", "unknown"),
        "messages": [messages_by_id[mid] for mid in message_ids],
    }
    for k, v in metadata.items():
        if k not in ("sessionId", "messages"):
            result[k] = v
    return result


# --------------------------------------------------------------------------- #
# Content helpers
# --------------------------------------------------------------------------- #
def _extract_text(content: Any) -> str:
    """Extract text from a Gemini content field (str or list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content) if content else ""


def _reasoning_from_thoughts(thoughts: list[dict[str, Any]] | None) -> str | None:
    """Join Gemini ``thoughts`` ({subject, description}) into reasoning text."""
    if not thoughts:
        return None
    parts: list[str] = []
    for thought in thoughts:
        subject = thought.get("subject", "")
        description = thought.get("description", "")
        if subject and description:
            parts.append(f"{subject}: {description}")
        elif description:
            parts.append(description)
    return "\n".join(parts) or None


def _observation_content(result: list[Any] | None) -> str | None:
    """Extract observation text from a toolCall ``result`` array.

    Image parts fold to a ``[image]`` placeholder rather than being
    saved to disk (adapter stays pure); add real image handling only if a run
    actually produces images.
    """
    if not result:
        return None
    text_output: str | None = None
    has_image = False
    for res_item in result:
        if not isinstance(res_item, dict):
            continue
        func_resp = res_item.get("functionResponse", {})
        response = func_resp.get("response", {})
        output = response.get("output")
        if output:
            text_output = output if isinstance(output, str) else _stringify(output)
        for part in func_resp.get("parts", []) or []:
            if isinstance(part, dict) and part.get("inlineData"):
                has_image = True
    if has_image and text_output:
        return f"{text_output}\n[image]"
    if has_image:
        return "[image]"
    return text_output


# --------------------------------------------------------------------------- #
# Conversion — ported from Harbor's _convert_gemini_to_atif
# --------------------------------------------------------------------------- #
def _convert(gemini_trajectory: dict[str, Any]) -> Trajectory | None:
    session_id = gemini_trajectory.get("sessionId", "unknown")
    messages = gemini_trajectory.get("messages", [])
    if not messages:
        return None

    steps: list[Step] = []
    step_id = 1
    total_input = 0
    total_output = 0
    total_cached = 0

    for message in messages:
        msg_type = message.get("type")
        timestamp = message.get("timestamp")

        if msg_type == "user":
            steps.append(
                Step(
                    step_id=step_id,
                    timestamp=timestamp,
                    source="user",
                    message=_extract_text(message.get("content", "")),
                )
            )
            step_id += 1

        elif msg_type == "gemini":
            content = _extract_text(message.get("content", ""))
            reasoning_content = _reasoning_from_thoughts(message.get("thoughts"))
            tool_calls_data = message.get("toolCalls", [])
            tokens = message.get("tokens", {})
            model_name = message.get("model")

            tool_calls: list[ToolCall] | None = None
            observation: Observation | None = None
            if tool_calls_data:
                tool_calls = []
                observation_results: list[ObservationResult] = []
                for tc in tool_calls_data:
                    tool_call_id = tc.get("id", "")
                    tool_calls.append(
                        ToolCall(
                            tool_call_id=tool_call_id,
                            function_name=tc.get("name", ""),
                            arguments=tc.get("args", {}) or {},
                        )
                    )
                    observation_results.append(
                        ObservationResult(
                            source_call_id=tool_call_id or None,
                            content=_observation_content(tc.get("result")),
                        )
                    )
                if observation_results:
                    observation = Observation(results=observation_results)

            metrics: Metrics | None = None
            if tokens:
                input_tokens = tokens.get("input", 0)
                output_tokens = tokens.get("output", 0)
                cached_tokens = tokens.get("cached", 0)
                thoughts_tokens = tokens.get("thoughts", 0)
                tool_tokens = tokens.get("tool", 0)
                completion_tokens = output_tokens + thoughts_tokens + tool_tokens
                total_input += input_tokens
                total_output += completion_tokens
                total_cached += cached_tokens
                metrics = Metrics(
                    prompt_tokens=input_tokens,
                    completion_tokens=completion_tokens,
                    cached_tokens=cached_tokens,
                    extra={"thoughts_tokens": thoughts_tokens, "tool_tokens": tool_tokens},
                )

            steps.append(
                Step(
                    step_id=step_id,
                    timestamp=timestamp,
                    source="agent",
                    model_name=model_name,
                    message=content,
                    reasoning_content=reasoning_content,
                    tool_calls=tool_calls,
                    observation=observation,
                    metrics=metrics,
                    llm_call_count=1,
                )
            )
            step_id += 1

    if not steps:
        return None

    default_model: str | None = None
    for step in steps:
        if step.source == "agent" and step.model_name:
            default_model = step.model_name
            break

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        agent=Agent(name=AGENT_NAME, version="unknown", model_name=default_model),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=total_input or None,
            total_completion_tokens=total_output or None,
            total_cached_tokens=total_cached or None,
            total_steps=len(steps),
        ),
    )


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert a Gemini CLI run directory into a validated ATIF ``Trajectory``.

    Args:
        run_dir: Canonical run directory
            (``results/<batch>/gemini/<problem_id>/run_<n>/``) containing an
            archived ``sessions/**/session-*.json``.
        sregym_meta: Optional SREGym metadata to attach under ``extra.sregym``.

    Returns:
        A validated ``Trajectory``, or ``None`` if no convertible session exists.
    """
    run_dir = Path(run_dir)
    session_file = _find_session_file(run_dir)
    if session_file is None:
        logger.debug("No Gemini session JSON found in %s", run_dir)
        return None

    gemini_trajectory = _load_gemini_session(session_file)
    if gemini_trajectory is None:
        logger.debug("Could not parse Gemini session at %s", session_file)
        return None

    trajectory = _convert(gemini_trajectory)
    if trajectory is None:
        return None

    if sregym_meta:
        trajectory.extra = {"sregym": dict(sregym_meta)}

    return trajectory
