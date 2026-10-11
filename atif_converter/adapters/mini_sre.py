"""mini-sre agent -> ATIF v1.7 adapter.

The mini-sre agent is SREGym's own reference client, so — like Stratus, and unlike
claudecode/codex/opencode/copilot — there is no Harbor converter to port. This adapter is
bespoke, built from the transcript the driver writes.

Input: ``mini_sre_transcript.jsonl``, written by ``clients/mini_sre/driver.py``. NDJSON:

    {"type":"meta", "backend":..., "model":..., "protocol":"mini", "submit_mode":..., ...}
    {"type":"prompt", "messages":[{"role":"system", "content":...}, {"role":"user", "content":...}]}
    {"type":"model_call", "stage":"diagnosis", "call":1, "content":..., "action":...,
     "usage":{...}, "finish_reason":..., "latency_s":..., "error":null}
    {"type":"command", "stage":"diagnosis", "index":1, "command":..., "exit_code":0,
     "stdout":{"text":..., "chars":..., "sha256":..., "stored_truncated":false}, "stderr":{...}}
    {"type":"submit", "stage":"diagnosis", "by":"command"|"driver"}
    {"type":"end", "stage":"diagnosis", "reason":"submitted_by_command"}
    ... the same again for the mitigation stage, then {"type":"end", "stage":"run", ...}

Key facts (confirmed against real runs):

- **One model call is one step.** The mini protocol allows exactly one action per reply, so a
  ``model_call`` and the ``command`` that follows it belong to the same step: the command becomes
  that step's single tool call and its output the step's observation. A reply the protocol could
  not use (no action, two actions, a provider error) is a step with no tool call.
- **The prompt** (the system prompt and the first message) becomes the first steps, with
  ``source`` system and user, as the other agents' trajectories begin. Transcripts written
  before the driver recorded it have no ``prompt`` record and start with the first reply.
- **Reasoning** is written per step to ``steps/step_NN/reasoning.txt`` rather than into the
  transcript, so it is picked up from the run directory when one is given.
- **Stages** are sequential phases (``diagnosis``, ``mitigation``). They are concatenated into one
  trajectory, with the boundaries and how each ended recorded under ``extra.mini_sre.stages``.
- **Token usage** is already SREGym's token-metrics v2, so it maps to ATIF ``Metrics`` directly.
- **Streams** are stored with a length and a sha256, and the text is cut at the driver's storage
  limit; a cut stream keeps its ``chars``/``sha256`` under the observation result's ``extra``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..atif import (
    Agent,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)
from ..errors import ConversionFailedError
from ._common import TOKEN_METRICS_VERSION, _aggregate_final_metrics, _load_jsonl, _stringify

logger = logging.getLogger(__name__)

AGENT_NAME = "mini-sre"
TRANSCRIPT_NAME = "mini_sre_transcript.jsonl"


def _metrics(usage: dict[str, Any] | None) -> Metrics | None:
    """Map one usage record (token metrics v2) to ATIF ``Metrics``."""
    if not isinstance(usage, dict) or not usage:
        return None
    extra = {
        key: usage[key]
        for key in ("reasoning_output_tokens", "cache_creation_input_tokens", "total_tokens")
        if usage.get(key) is not None
    }
    metrics = Metrics(
        prompt_tokens=usage.get("input_tokens"),
        completion_tokens=usage.get("output_tokens"),
        cached_tokens=usage.get("cached_input_tokens"),
        extra={"token_metrics_version": usage.get("token_metrics_version", TOKEN_METRICS_VERSION), **extra},
    )
    return metrics


def _stream_text(stream: Any) -> str:
    """Return the stored text of a stdout or stderr record."""
    return stream.get("text", "") if isinstance(stream, dict) else _stringify(stream)


def _stream_extra(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return the length and hash of streams that were cut when stored."""
    extra: dict[str, Any] = {}
    for name in ("stdout", "stderr"):
        stream = record.get(name)
        if isinstance(stream, dict) and stream.get("stored_truncated"):
            extra[name] = {"chars": stream.get("chars"), "sha256": stream.get("sha256"), "stored_truncated": True}
    return extra or None


def _observation(record: dict[str, Any], call_id: str) -> Observation:
    """Build the observation for one command: the return code, then stdout and stderr."""
    stdout = _stream_text(record.get("stdout"))
    stderr = _stream_text(record.get("stderr"))
    body = "\n".join(part for part in (stdout.rstrip("\n"), stderr.rstrip("\n")) if part)
    content = (
        f"<returncode>{record.get('exit_code')}</returncode>\n{body}"
        if body
        else f"<returncode>{record.get('exit_code')}</returncode>"
    )
    extra: dict[str, Any] = {
        key: record[key]
        for key in ("duration_s", "timed_out", "refused", "submission_attempt")
        if record.get(key) is not None
    }
    streams = _stream_extra(record)
    if streams:
        extra["streams"] = streams
    return Observation(results=[ObservationResult(source_call_id=call_id, content=content, extra=extra or None)])


def _tool_call(record: dict[str, Any], call_id: str) -> ToolCall:
    """A ``command`` record is a bash call."""
    return ToolCall(tool_call_id=call_id, function_name="bash", arguments={"command": record.get("command") or ""})


def _reasoning(run_dir: Path | None, call_number: int) -> str | None:
    """Read a step's reasoning from steps/step_NN/reasoning.txt, if it exists."""
    if run_dir is None:
        return None
    path = run_dir / "steps" / f"step_{call_number:02d}" / "reasoning.txt"
    try:
        return path.read_text(encoding="utf-8") or None
    except OSError:
        return None


def convert_records(
    records: list[dict[str, Any]], *, run_dir: Path | None = None, agent_version: str | None = None
) -> Trajectory:
    """Build one ATIF trajectory from the transcript's records."""
    meta = next((r for r in records if r.get("type") == "meta"), {})
    steps: list[Step] = []
    stages: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None  # the model call whose action has not been seen yet

    def flush(action_record: dict[str, Any] | None) -> None:
        """Emit the pending model call, with the action it produced if there was one."""
        nonlocal pending
        if pending is None:
            return
        call_number = pending.get("call") or len(steps) + 1
        call_id = f"{pending.get('stage', 'diagnosis')}-{call_number}"
        tool_calls = None
        observation = None
        if action_record is not None:
            tool_calls = [_tool_call(action_record, call_id)]
            observation = _observation(action_record, call_id)
        extra = {
            key: pending[key]
            for key in ("stage", "call", "finish_reason", "latency_s", "error", "content_from_reasoning")
            if pending.get(key) is not None
        }
        steps.append(
            Step(
                step_id=len(steps) + 1,
                timestamp=pending.get("ts"),
                source="agent",
                model_name=meta.get("model"),
                reasoning_effort=meta.get("reasoning_effort"),
                message=pending.get("content") or "",
                reasoning_content=_reasoning(run_dir, call_number),
                tool_calls=tool_calls,
                observation=observation,
                metrics=_metrics(pending.get("usage")),
                llm_call_count=1,  # the mini protocol is one model call per step
                extra=extra or None,
            )
        )
        pending = None

    for record in records:
        kind = record.get("type")
        if kind == "prompt":
            for message in record.get("messages") or []:
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        timestamp=record.get("ts"),
                        source="system" if message.get("role") == "system" else "user",
                        message=_stringify(message.get("content")),
                    )
                )
        elif kind == "model_call":
            flush(None)  # a reply with no usable action still had its turn
            pending = record
        elif kind == "command":
            flush(record)
        elif kind == "end":
            flush(None)
            if record.get("stage") != "run":
                stages.append({"stage": record.get("stage"), "reason": record.get("reason"), "steps": len(steps)})
    flush(None)

    if not any(step.source == "agent" for step in steps):
        raise ConversionFailedError("mini-sre transcript holds no model calls")

    submissions = [{"stage": r.get("stage"), "by": r.get("by")} for r in records if r.get("type") == "submit"]
    run_end = next((r for r in records if r.get("type") == "end" and r.get("stage") == "run"), {})
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=meta.get("artifact_id"),
        agent=Agent(
            name=AGENT_NAME,
            version=agent_version or meta.get("backend_version") or "unknown",
            model_name=meta.get("model"),
            extra={
                key: meta[key]
                for key in (
                    "backend",
                    "protocol",
                    "submit_mode",
                    "submission_mode",
                    "max_commands",
                    "hard_cap",
                    "deadline_s",  # transcripts written before the time was split between the stages
                    "attempt_s",
                    "diagnosis_share",
                    "command_timeout_s",
                    "reasoning_effort",
                    "tools",
                )
                if meta.get(key) is not None
            },
        ),
        steps=steps,
        final_metrics=_aggregate_final_metrics(steps),
        extra={
            "mini_sre": {
                "stages": stages,
                "submissions": submissions,
                "return_code": run_end.get("return_code"),
            }
        },
    )


def convert_file(session_file: Path | str, *, agent_version: str | None = None) -> Trajectory:
    """Convert one ``mini_sre_transcript.jsonl``; reasoning is read from its run directory."""
    path = Path(session_file)
    return convert_records(_load_jsonl(path), run_dir=path.parent, agent_version=agent_version)
