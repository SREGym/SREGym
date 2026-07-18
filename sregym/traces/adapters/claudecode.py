"""Claude Code -> ATIF v1.7 adapter.

A clean port of Harbor's ``ClaudeCode._convert_events_to_trajectory`` and its
helpers (upstream commit ``fd1a8ea``; see ``sregym/traces/atif/UPSTREAM.md``)
into standalone, pure functions with no dependency on ``harbor`` or
``BaseInstalledAgent``.

The conversion reads a single run directory (the canonical
``results/<batch>/claudecode/<problem_id>/run_<n>/``) and produces one validated
ATIF ``Trajectory`` covering the whole session.

Key behavior ported verbatim from Harbor:

- **message-id coalescing**: one LLM inference emits several session-log events
  (separate ``thinking`` / ``text`` / ``tool_use`` blocks) that all share a
  ``message.id``. They are bundled into a single ATIF step via ``turn_by_msgid``,
  and usage is counted once per ``message.id`` (``last_usage_by_msg_id`` +
  ``seen_message_ids``). Without this, steps and token totals inflate.
- **usage lives in ``message.usage``**, not top-level.
- **cost** is read from the ``{"type":"result", "total_cost_usd": ...}`` line in
  ``claude-code.txt`` (the only place it appears).

The one intentional deviation from Harbor: ``agent.extra`` set-valued fields
(``cwds`` / ``git_branches`` / ``agent_ids``) are coerced to sorted lists so the
result is JSON-serializable via ``Trajectory.to_json_dict()``.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from sregym.traces.adapters._common import (
    _aggregate_final_metrics,
    _load_jsonl,
    _stringify,
)
from sregym.traces.atif import (
    Agent,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "claudecode"


# --------------------------------------------------------------------------- #
# Session-directory discovery
# --------------------------------------------------------------------------- #
def _get_session_dir(run_dir: Path) -> Path | None:
    """Identify the Claude session directory containing the primary JSONL log.

    Ported from Harbor's ``_get_session_dir``: looks under
    ``<run_dir>/sessions/projects/`` for a project dir containing ``*.jsonl``
    files (excluding ``subagents`` parents).
    """
    sessions_root = run_dir / "sessions"
    if not sessions_root.exists():
        return None

    project_root = sessions_root / "projects"
    if not project_root.is_dir():
        return None

    all_session_dirs: list[Path] = []
    for project_dir in project_root.iterdir():
        if not project_dir.is_dir():
            continue
        jsonl_files = list(project_dir.rglob("*.jsonl"))
        if not jsonl_files:
            continue
        session_dirs = list({f.parent for f in jsonl_files if "subagents" not in f.parent.parts})
        all_session_dirs.extend(session_dirs)

    if not all_session_dirs:
        return None
    if len(all_session_dirs) == 1:
        return all_session_dirs[0]

    logger.debug(
        "Multiple Claude Code session directories found in %s; could not identify the correct one",
        run_dir,
    )
    return None


# --------------------------------------------------------------------------- #
# Content extraction helpers (ported verbatim)
# --------------------------------------------------------------------------- #
def _extract_text_reasoning_tool_uses(
    content: Any,
) -> tuple[str, str | None, list[dict[str, Any]]]:
    if isinstance(content, str):
        return content.strip(), None, []

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_blocks: list[dict[str, Any]] = []

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                text_parts.append(_stringify(block))
                continue

            block_type = block.get("type")
            if block_type == "tool_use":
                tool_blocks.append(block)
                continue

            if block_type in {"thinking", "reasoning", "analysis"}:
                text_value = block.get("text") if block.get("text") is not None else block.get("thinking")
                if isinstance(text_value, str):
                    reasoning_parts.append(text_value.strip())
                else:
                    reasoning_parts.append(_stringify(text_value))
                continue

            if block_type == "redacted_thinking":
                data = block.get("data")
                if isinstance(data, str) and data.startswith("openrouter.reasoning:"):
                    try:
                        payload = data[len("openrouter.reasoning:") :]
                        decoded = base64.b64decode(payload + "==").decode("utf-8", "replace")
                        inner = json.loads(decoded)
                        inner_text = inner.get("text")
                        if isinstance(inner_text, str):
                            reasoning_parts.append(inner_text.strip())
                    except (ValueError, json.JSONDecodeError):
                        pass
                continue

            if block_type == "code" and isinstance(block.get("code"), str):
                text_parts.append(block["code"])
                continue

            text_value = block.get("text")
            if isinstance(text_value, str):
                text_parts.append(text_value)
            else:
                text_parts.append(_stringify(block))
    elif content is not None:
        text_parts.append(_stringify(content))

    text = "\n\n".join(p.strip() for p in text_parts if p and str(p).strip())
    reasoning = "\n\n".join(p.strip() for p in reasoning_parts if p and str(p).strip())
    return text, (reasoning or None), tool_blocks


def _build_metrics(usage: Any) -> Metrics | None:
    if not isinstance(usage, dict):
        return None

    cached_tokens = usage.get("cache_read_input_tokens") or 0
    creation = usage.get("cache_creation_input_tokens") or 0
    input_tokens = usage.get("input_tokens") or 0
    prompt_tokens = input_tokens + cached_tokens + creation
    completion_tokens = usage.get("output_tokens") or 0

    extra: dict[str, Any] = {}
    for key, value in usage.items():
        if key in {"input_tokens", "output_tokens"}:
            continue
        extra[key] = value

    return Metrics(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        cost_usd=None,
        extra=extra or None,
    )


def _format_tool_result(
    block: dict[str, Any], tool_use_result: dict[str, Any] | None
) -> tuple[str | None, dict[str, Any] | None]:
    parts: list[str] = []

    content = block.get("content")
    if isinstance(content, str):
        if content.strip():
            parts.append(content.strip())
    elif isinstance(content, list):
        for item in content:
            text_value = _stringify(item)
            if text_value.strip():
                parts.append(text_value.strip())
    elif content not in (None, ""):
        parts.append(_stringify(content))

    metadata: dict[str, Any] | None = None
    if tool_use_result and isinstance(tool_use_result, dict):
        metadata = {"tool_use_result": tool_use_result}
        stdout = tool_use_result.get("stdout")
        stderr = tool_use_result.get("stderr")
        exit_code = tool_use_result.get("exitCode") or tool_use_result.get("exit_code")
        interrupted = tool_use_result.get("interrupted")
        is_image = tool_use_result.get("isImage")

        formatted_chunks: list[str] = []
        if stdout:
            formatted_chunks.append(f"[stdout]\n{stdout}".rstrip())
        if stderr:
            formatted_chunks.append(f"[stderr]\n{stderr}".rstrip())
        if exit_code not in (None, 0):
            formatted_chunks.append(f"[exit_code] {exit_code}")
        if interrupted:
            formatted_chunks.append(f"[interrupted] {interrupted}")
        if is_image:
            formatted_chunks.append(f"[is_image] {is_image}")

        remaining_meta = {
            key: value
            for key, value in tool_use_result.items()
            if key not in {"stdout", "stderr", "exitCode", "exit_code", "interrupted", "isImage"}
        }
        if remaining_meta:
            formatted_chunks.append(f"[metadata] {json.dumps(remaining_meta, ensure_ascii=False)}")

        if formatted_chunks:
            parts.append("\n".join(chunk for chunk in formatted_chunks if chunk))

    if block.get("is_error") is True:
        parts.append("[error] tool reported failure")
        metadata = metadata or {}
        metadata["is_error"] = True

    if metadata is not None:
        metadata.setdefault("raw_tool_result", block)

    result_text = "\n\n".join(part for part in parts if part).strip()
    return (result_text or None), metadata


def _parse_total_cost_from_stream_json(run_dir: Path) -> float | None:
    """Extract authoritative ``total_cost_usd`` from ``claude-code.txt``."""
    stream_path = run_dir / "claude-code.txt"
    try:
        content = stream_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            cost = event.get("total_cost_usd")
            if cost is None:
                return None
            try:
                return float(cost)
            except (TypeError, ValueError):
                return None
    return None


# --------------------------------------------------------------------------- #
# Normalized-event -> Step
# --------------------------------------------------------------------------- #
def _convert_event_to_step(event: dict[str, Any], step_id: int, default_model_name: str | None) -> Step:
    kind = event.get("kind")
    timestamp = event.get("timestamp")

    if kind == "message":
        # The normalizer only emits ``kind:"message"`` for user turns
        # (assistant turns become ``agent_step``; there is no system-message
        # producer). Keep this branch user-only rather than carrying dead
        # assistant/system arms.
        text = event.get("text", "")
        extra = event.get("extra")
        step = Step(
            step_id=step_id,
            timestamp=timestamp,
            source="user",
            message=text,
        )
        if extra:
            step.extra = extra
        return step

    if kind == "agent_step":
        text = event.get("text") or ""
        reasoning = event.get("reasoning")
        metrics = event.get("metrics")
        extra = event.get("extra")
        model_name = event.get("model_name") or default_model_name
        tool_specs = event.get("tool_calls") or []

        tool_calls: list[ToolCall] = []
        results: list[ObservationResult] = []
        for spec in tool_specs:
            spec_call_id = spec.get("call_id")
            if not spec_call_id:
                continue
            tool_calls.append(
                ToolCall(
                    tool_call_id=spec_call_id,
                    function_name=spec.get("tool_name") or "",
                    arguments=spec.get("arguments") or {},
                    extra=spec.get("extra"),
                )
            )
            if spec.get("output") is not None:
                results.append(
                    ObservationResult(
                        source_call_id=spec_call_id,
                        content=spec.get("output"),
                        subagent_trajectory_ref=None,
                        extra=spec.get("result_extra"),
                    )
                )

        step = Step(
            step_id=step_id,
            timestamp=timestamp,
            source="agent",
            message=text,
            tool_calls=tool_calls or None,
            observation=Observation(results=results) if results else None,
            llm_call_count=1,
        )
        if reasoning:
            step.reasoning_content = reasoning
        if model_name:
            step.model_name = model_name
        if metrics:
            step.metrics = metrics
        if extra:
            step.extra = extra
        return step

    if kind == "tool_call":
        # Reached only for ORPHAN tool_results: a ``tool_result`` whose
        # originating ``tool_use`` never appeared in this window (e.g. replayed
        # after compaction). The normalizer emits this with only call_id /
        # tool_name / output / extra set; the ``reasoning`` / ``metrics`` /
        # ``status`` / ``raw_arguments`` handling below is defensive and is not
        # populated by that producer today.
        call_id = event.get("call_id")
        tool_name = event.get("tool_name")
        if not call_id or not tool_name:
            raise ValueError("Tool call event missing call_id or tool_name")

        arguments = event.get("arguments") or {}
        reasoning = event.get("reasoning")
        metrics = event.get("metrics")
        extra = event.get("extra")
        status = event.get("status")
        message = event.get("message")
        output = event.get("output")
        metadata = event.get("metadata")
        raw_arguments = event.get("raw_arguments")
        model_name = event.get("model_name") or default_model_name

        tool_call = ToolCall(
            tool_call_id=call_id,
            function_name=tool_name,
            arguments=arguments,
        )
        observation_result = ObservationResult(
            source_call_id=call_id,
            content=output,
            subagent_trajectory_ref=None,
        )
        observation = Observation(results=[observation_result]) if output is not None else None

        extra = extra or {}
        for key, value in {
            "metadata": metadata,
            "raw_arguments": raw_arguments,
            "status": status,
        }.items():
            if value is not None:
                extra.setdefault(key, value)

        step = Step(
            step_id=step_id,
            timestamp=timestamp,
            source="agent",
            message=message or "",
            tool_calls=[tool_call],
            observation=observation,
            llm_call_count=1,
        )
        if model_name:
            step.model_name = model_name
        if reasoning:
            step.reasoning_content = reasoning
        if metrics:
            step.metrics = metrics
        if extra:
            step.extra = extra
        return step

    raise ValueError(f"Unsupported event kind '{kind}'")


# --------------------------------------------------------------------------- #
# Main conversion
# --------------------------------------------------------------------------- #
def _convert_session(session_dir: Path, run_dir: Path) -> Trajectory | None:
    session_files = list(session_dir.glob("*.jsonl"))
    if not session_files:
        logger.debug("No Claude Code session files found in %s", session_dir)
        return None

    raw_events: list[dict[str, Any]] = []
    for session_file in session_files:
        raw_events.extend(_load_jsonl(session_file))

    if not raw_events:
        return None

    # Dedupe by event uuid.
    seen_event_uuids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for event in raw_events:
        uuid = event.get("uuid")
        if isinstance(uuid, str) and uuid:
            if uuid in seen_event_uuids:
                continue
            seen_event_uuids.add(uuid)
        deduped.append(event)
    raw_events = deduped

    raw_events.sort(key=lambda e: e.get("timestamp", ""))
    events = [e for e in raw_events if e.get("isSidechain")] + [e for e in raw_events if not e.get("isSidechain")]
    if not events:
        return None

    session_id = session_dir.name
    for event in events:
        sid = event.get("sessionId")
        if isinstance(sid, str):
            session_id = sid
            break

    agent_version = "unknown"
    for event in events:
        ver = event.get("version")
        if isinstance(ver, str) and ver:
            agent_version = ver
            break

    cwds = {e.get("cwd") for e in events if isinstance(e.get("cwd"), str) and e.get("cwd")}
    git_branches = {e.get("gitBranch") for e in events if isinstance(e.get("gitBranch"), str) and e.get("gitBranch")}
    agent_ids = {e.get("agentId") for e in events if isinstance(e.get("agentId"), str) and e.get("agentId")}

    # Deviation from Harbor: coerce sets -> sorted lists so to_json_dict() works.
    agent_extra: dict[str, Any] | None = {}
    if cwds:
        agent_extra["cwds"] = sorted(cwds)
    if git_branches:
        agent_extra["git_branches"] = sorted(git_branches)
    if agent_ids:
        agent_extra["agent_ids"] = sorted(agent_ids)
    if not agent_extra:
        agent_extra = None

    default_model_name: str | None = None
    for event in events:
        message = event.get("message")
        if isinstance(message, dict):
            model_name = message.get("model")
            if isinstance(model_name, str) and model_name:
                default_model_name = model_name
                break

    # Per message id, keep the last usage (streaming updates it each chunk).
    last_usage_by_msg_id: dict[str, Any] = {}
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        usage = msg.get("usage")
        if mid and usage is not None:
            last_usage_by_msg_id[mid] = usage

    normalized_events: list[dict[str, Any]] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    completed_call_ids: set[str] = set()
    seen_message_ids: set[str] = set()
    turn_by_msgid: dict[str, dict[str, Any]] = {}

    for event in events:
        message = event.get("message")
        if not isinstance(message, dict):
            continue

        event_type = event.get("type")
        timestamp = event.get("timestamp")

        if event_type == "assistant":
            text, reasoning, tool_blocks = _extract_text_reasoning_tool_uses(message.get("content"))

            msg_id = message.get("id")
            if msg_id and msg_id in seen_message_ids:
                metrics = None
            else:
                usage = last_usage_by_msg_id.get(msg_id, message.get("usage")) if msg_id else message.get("usage")
                metrics = _build_metrics(usage)
                if msg_id:
                    seen_message_ids.add(msg_id)

            extra: dict[str, Any] = {}
            for key in ("stop_reason", "stop_sequence", "requestId"):
                value = message.get(key)
                if value is not None:
                    extra[key] = value
            if event.get("cwd"):
                extra.setdefault("cwd", event["cwd"])
            if event.get("userType") and event.get("userType") != "external":
                extra["user_type"] = event["userType"]
            extra["is_sidechain"] = event.get("isSidechain", False)

            model_name = message.get("model") or default_model_name

            turn = turn_by_msgid.get(msg_id) if msg_id else None
            if turn is None:
                turn = {
                    "kind": "agent_step",
                    "timestamp": timestamp,
                    "text": "",
                    "reasoning": None,
                    "metrics": None,
                    "extra": extra or None,
                    "model_name": model_name,
                    "tool_calls": [],
                }
                normalized_events.append(turn)
                if msg_id:
                    turn_by_msgid[msg_id] = turn

            if text:
                turn["text"] = f"{turn['text']}\n\n{text}".strip() if turn["text"] else text
            if reasoning and message.get("role") == "assistant":
                turn["reasoning"] = f"{turn['reasoning']}\n\n{reasoning}" if turn["reasoning"] else reasoning
            if turn["metrics"] is None and metrics is not None:
                turn["metrics"] = metrics

            turn_calls = turn["tool_calls"]
            for tool_block in tool_blocks:
                call_id = tool_block.get("id") or tool_block.get("tool_use_id")
                if not call_id:
                    continue
                if call_id in pending_calls or call_id in completed_call_ids:
                    continue

                raw_arguments = tool_block.get("input")
                arguments = raw_arguments if isinstance(raw_arguments, dict) else {"input": raw_arguments}

                call_extra: dict[str, Any] = {}
                if raw_arguments is not None:
                    call_extra["raw_arguments"] = raw_arguments
                if tool_block.get("status") is not None:
                    call_extra["status"] = tool_block.get("status")
                if tool_block.get("is_error") is not None:
                    call_extra["tool_use_is_error"] = tool_block.get("is_error")
                if tool_block.get("name"):
                    call_extra.setdefault("tool_use_name", tool_block.get("name"))

                tool_call_spec: dict[str, Any] = {
                    "call_id": call_id,
                    "tool_name": tool_block.get("name") or "",
                    "arguments": arguments or {},
                    "extra": call_extra or None,
                    "output": None,
                    "result_extra": None,
                }
                turn_calls.append(tool_call_spec)
                pending_calls[call_id] = tool_call_spec
            continue

        if event_type == "user":
            content = message.get("content")
            if isinstance(content, str):
                if content.strip():
                    normalized_events.append(
                        {
                            "kind": "message",
                            "timestamp": timestamp,
                            "role": "user",
                            "text": content,
                            "extra": {"is_sidechain": event.get("isSidechain", False)},
                        }
                    )
                continue

            if isinstance(content, list):
                text_parts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
                        continue

                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        call_id = block.get("tool_use_id")
                        formatted_output, metadata = _format_tool_result(block, event.get("toolUseResult"))
                        call_info = pending_calls.pop(call_id, None) if call_id else None
                        if call_info is not None:
                            result_extra: dict[str, Any] = {}
                            if metadata:
                                result_extra["tool_result_metadata"] = metadata
                            if block.get("is_error") is not None:
                                result_extra["tool_result_is_error"] = block.get("is_error")
                            call_info["output"] = formatted_output
                            call_info["result_extra"] = result_extra or None
                            if call_id:
                                completed_call_ids.add(call_id)
                            continue

                        if call_id and call_id in completed_call_ids:
                            continue
                        tool_name = block.get("name") or block.get("tool_name") or ""
                        if not tool_name:
                            logger.debug(
                                "Skipping orphan Claude Code tool result %s without tool name",
                                call_id or "<missing>",
                            )
                            continue
                        orphan_extra: dict[str, Any] = {}
                        if metadata:
                            orphan_extra["tool_result_metadata"] = metadata
                        if block.get("is_error") is not None:
                            orphan_extra["tool_result_is_error"] = block.get("is_error")
                        normalized_events.append(
                            {
                                "kind": "tool_call",
                                "timestamp": timestamp,
                                "call_id": call_id or "",
                                "tool_name": tool_name,
                                "arguments": {},
                                "raw_arguments": None,
                                "reasoning": None,
                                "status": None,
                                "message": None,
                                "extra": orphan_extra or None,
                                "metadata": metadata,
                                "metrics": None,
                                "model_name": default_model_name,
                                "output": formatted_output,
                            }
                        )
                        if call_id:
                            completed_call_ids.add(call_id)
                        continue

                    text_parts.append(_stringify(block))

                text_message = "\n\n".join(p for p in text_parts if p.strip())
                if text_message:
                    normalized_events.append(
                        {
                            "kind": "message",
                            "timestamp": timestamp,
                            "role": "user",
                            "text": text_message,
                        }
                    )
                continue

            if content not in (None, ""):
                text = _stringify(content)
                if text.strip():
                    normalized_events.append(
                        {
                            "kind": "message",
                            "timestamp": timestamp,
                            "role": "user",
                            "text": text,
                        }
                    )

    steps: list[Step] = []
    for norm_event in normalized_events:
        try:
            step = _convert_event_to_step(norm_event, len(steps) + 1, default_model_name)
        except ValueError as exc:
            logger.debug("Skipping event during step conversion: %s", exc)
            continue
        if step.source == "agent" and not step.model_name and default_model_name:
            step.model_name = default_model_name
        steps.append(step)

    if not steps:
        logger.debug("No valid steps produced from Claude Code session")
        return None

    service_tiers: set[str] = set()
    cache_creation_total, cache_read_total = 0, 0
    cache_creation_seen, cache_read_seen = False, False
    for step in steps:
        if not step.metrics or not step.metrics.extra:
            continue
        meta = step.metrics.extra
        tier = meta.get("service_tier")
        if isinstance(tier, str):
            service_tiers.add(tier)
        cc = meta.get("cache_creation_input_tokens")
        if isinstance(cc, int):
            cache_creation_total += cc
            cache_creation_seen = True
        cr = meta.get("cache_read_input_tokens")
        if isinstance(cr, int):
            cache_read_total += cr
            cache_read_seen = True

    final_extra: dict[str, Any] | None = {}
    if service_tiers:
        final_extra["service_tiers"] = sorted(service_tiers)
    if cache_creation_seen:
        final_extra["total_cache_creation_input_tokens"] = cache_creation_total
    if cache_read_seen:
        final_extra["total_cache_read_input_tokens"] = cache_read_total
    if not final_extra:
        final_extra = None

    final_metrics = _aggregate_final_metrics(
        steps,
        total_cost_usd=_parse_total_cost_from_stream_json(run_dir),
        extra=final_extra,
    )

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
    """Convert a Claude Code run directory into a validated ATIF ``Trajectory``.

    Args:
        run_dir: Canonical run directory
            (``results/<batch>/claudecode/<problem_id>/run_<n>/``).
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
        logger.debug("No Claude Code session directory found in %s", run_dir)
        return None

    trajectory = _convert_session(session_dir, run_dir)
    if trajectory is None:
        return None

    if sregym_meta:
        trajectory.extra = {"sregym": dict(sregym_meta)}

    return trajectory
