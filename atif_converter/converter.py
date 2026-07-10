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
    """Load a single JSON object or a bounded prefix of a JSONL stream."""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise UnsupportedFormatError(f"session file is empty: {path}")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        return payload, [payload]
    if isinstance(payload, list):
        records = [item for item in payload if isinstance(item, dict)]
        return None, records[:200]

    records: list[dict] = []
    for line in text.splitlines():
        if len(records) >= 200:
            break
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    if not records:
        raise UnsupportedFormatError(f"session file contains no recognizable JSON records: {path}")
    return None, records


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
    dotted_types = {
        "assistant.message",
        "assistant.reasoning",
        "user.message",
        "tool.execution_complete",
        "result",
    }
    flat_types = {"message", "tool_use", "tool_result", "usage"}
    record_types = {record.get("type") for record in records}
    return bool(record_types & dotted_types) or bool(record_types & flat_types)


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
