"""Content-based dispatch for native agent session files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

from .adapters import claudecode, codex, copilot, gemini, opencode, stratus
from .atif import Trajectory
from .errors import (
    AtifConverterError,
    ConversionFailedError,
    UnsupportedAgentError,
    UnsupportedFormatError,
)

AgentName = Literal["claudecode", "codex", "copilot", "gemini", "opencode", "stratus"]
SUPPORTED_AGENTS: tuple[AgentName, ...] = (
    "claudecode",
    "codex",
    "copilot",
    "gemini",
    "opencode",
    "stratus",
)

_CONVERTERS = {
    "claudecode": claudecode.convert_file,
    "codex": codex.convert_file,
    "copilot": copilot.convert_file,
    "gemini": gemini.convert_file,
    "opencode": opencode.convert_file,
    "stratus": stratus.convert_file,
}


def _require_file(session_file: Path | str) -> Path:
    path = Path(session_file)
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise IsADirectoryError(path)
    return path


def _load_detection_records(path: Path) -> tuple[dict | None, list[dict]]:
    """Load a JSON document or inspect a bounded prefix of a JSONL stream."""
    records: list[dict] = []
    nonblank_lines = 0
    first_payload: object | None = None
    first_line_parsed = False
    first_line_starts_json = False
    prefix_truncated = False

    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue

                nonblank_lines += 1
                if nonblank_lines == 1:
                    first_line_starts_json = stripped[0] in "[{"

                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    payload = None
                else:
                    if nonblank_lines == 1:
                        first_payload = payload
                        first_line_parsed = True
                    if isinstance(payload, dict):
                        records.append(payload)

                if nonblank_lines >= 200:
                    prefix_truncated = True
                    break
    except FileNotFoundError:
        # Preserve the public API's normal missing-file behavior even if the
        # file disappears between _require_file() and this read.
        raise
    except (OSError, UnicodeError) as exc:
        raise UnsupportedFormatError(f"session file is not readable UTF-8 JSON: {path}") from exc

    if nonblank_lines == 0:
        raise UnsupportedFormatError(f"session file is empty: {path}")

    # A complete object on the first line is either a one-line JSON document or
    # the first JSONL record. Reaching another nonblank line distinguishes the
    # latter without loading the rest of a potentially large session file.
    if first_line_parsed and isinstance(first_payload, dict):
        if not prefix_truncated and nonblank_lines == 1:
            return first_payload, [first_payload]
        return None, records

    if first_line_parsed and isinstance(first_payload, list) and not prefix_truncated and nonblank_lines == 1:
        return None, [item for item in first_payload if isinstance(item, dict)][:200]

    # Non-JSON preamble lines occur in captured CLI streams. Once recognizable
    # records follow, treat the input as JSONL rather than rereading the file.
    if records and not first_line_starts_json:
        return None, records

    # A leading incomplete "{" or "[" normally means a pretty-printed JSON
    # document. These formats need the complete payload for structural checks.
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError) as exc:
        raise UnsupportedFormatError(f"session file is not readable UTF-8 JSON: {path}") from exc
    except json.JSONDecodeError:
        if records:
            return None, records
        raise UnsupportedFormatError(f"session file contains no recognizable JSON records: {path}") from None

    if isinstance(payload, dict):
        return payload, [payload]
    if isinstance(payload, list):
        records = [item for item in payload if isinstance(item, dict)]
        return None, records[:200]
    raise UnsupportedFormatError(f"session file contains no recognizable JSON records: {path}")


def _looks_like_opencode(root: dict | None) -> bool:
    if not root or not isinstance(root.get("info"), dict) or not isinstance(root.get("messages"), list):
        return False
    return any(isinstance(message, dict) and "info" in message and "parts" in message for message in root["messages"])


def _looks_like_gemini(root: dict | None, records: list[dict]) -> bool:
    if root and isinstance(root.get("messages"), list):
        message_types = {message.get("type") for message in root["messages"] if isinstance(message, dict)}
        if "gemini" in message_types or ("sessionId" in root and "user" in message_types):
            return True

    record_types = {record.get("type") for record in records}
    if "gemini" in record_types or "message_update" in record_types:
        return True
    return any("$set" in record for record in records) and any(
        "kind" in record and "sessionId" in record for record in records
    )


def _looks_like_stratus(records: list[dict]) -> bool:
    return any(
        record.get("type") == "event" and "stage" in record and isinstance(record.get("messages"), list)
        for record in records
    )


def _looks_like_codex(records: list[dict]) -> bool:
    return any(
        record.get("type") in {"session_meta", "response_item", "turn_context", "event_msg"}
        and isinstance(record.get("payload"), dict)
        for record in records
    )


def _looks_like_claudecode(records: list[dict]) -> bool:
    return any(
        record.get("type") in {"assistant", "user", "system"}
        and isinstance(record.get("message"), dict)
        and ("sessionId" in record or "uuid" in record)
        for record in records
    )


def _looks_like_copilot(records: list[dict]) -> bool:
    for record in records:
        record_type = record.get("type")
        data = record.get("data")

        # Copilot's session-event schema consistently wraps mapped event data
        # in an object. Event names alone (especially "result") are too
        # generic to identify the format safely.
        if record_type in {
            "assistant.message",
            "assistant.reasoning",
            "user.message",
            "tool.execution_complete",
        } and isinstance(data, dict):
            return True

        # The older flat schema uses generic event names, so also require the
        # fields that make each record structurally useful to the adapter.
        if record_type == "message" and record.get("role") in {"user", "assistant"} and "content" in record:
            return True
        if record_type == "tool_use" and isinstance(record.get("id"), str) and isinstance(record.get("name"), str):
            return True
        if record_type == "tool_result" and isinstance(record.get("tool_use_id"), str) and "content" in record:
            return True
        if record_type == "usage" and any(key in record for key in ("input_tokens", "output_tokens")):
            return True
    return False


def detect_agent(session_file: Path | str) -> AgentName:
    """Detect the originating agent from a native session file's structure."""
    path = _require_file(session_file)
    root, records = _load_detection_records(path)

    if _looks_like_opencode(root):
        return "opencode"
    if _looks_like_gemini(root, records):
        return "gemini"
    if _looks_like_stratus(records):
        return "stratus"
    if _looks_like_codex(records):
        return "codex"
    if _looks_like_claudecode(records):
        return "claudecode"
    if _looks_like_copilot(records):
        return "copilot"
    raise UnsupportedFormatError(f"could not detect an agent format for {path}")


def convert(session_file: Path | str, *, agent: AgentName | str | None = None) -> Trajectory:
    """Convert one native agent session file into a validated ATIF trajectory.

    The agent is detected from file contents unless ``agent`` explicitly selects
    one of :data:`SUPPORTED_AGENTS`.
    """
    path = _require_file(session_file)
    if agent is None:
        selected = detect_agent(path)
    elif agent not in _CONVERTERS:
        supported = ", ".join(SUPPORTED_AGENTS)
        raise UnsupportedAgentError(f"unsupported agent {agent!r}; expected one of: {supported}")
    else:
        selected = cast(AgentName, agent)

    try:
        trajectory = _CONVERTERS[selected](path)
    except AtifConverterError:
        raise
    except Exception as exc:
        raise ConversionFailedError(f"{selected} conversion failed for {path}: {exc}") from exc
    if trajectory is None:
        raise ConversionFailedError(f"{selected} converter produced no trajectory for {path}")
    return trajectory
