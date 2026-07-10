"""Public-contract tests for the copyable standalone ATIF converter."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from atif_converter import (
    ConversionFailedError,
    Trajectory,
    UnsupportedAgentError,
    UnsupportedFormatError,
    convert,
    detect_agent,
)

FIXTURES = Path(__file__).parent / "fixtures"

SESSION_CASES = [
    (
        "claudecode",
        FIXTURES / "claudecode_run" / "sessions" / "projects" / "-logs" / "74bfdd52-f1bd-477d-98c8-306bde810080.jsonl",
    ),
    (
        "codex",
        FIXTURES
        / "codex_run"
        / "sessions"
        / "2026"
        / "06"
        / "29"
        / "rollout-2026-06-29T16-58-06-019f1451-1903-7092-b4ca-73a57d9bdd9d.jsonl",
    ),
    ("copilot", FIXTURES / "copilot_run_flat" / "copilot-cli.jsonl"),
    ("copilot", FIXTURES / "copilot_run_real" / "copilot-cli.jsonl"),
    (
        "gemini",
        FIXTURES / "gemini_run" / "sessions" / "2026" / "07" / "04" / "session-fdaf509c.jsonl",
    ),
    (
        "opencode",
        FIXTURES / "opencode_run" / "sessions" / "2026" / "06" / "30" / "session-ses_0e8fabe63ffe91KaErs7Mh9g5O.json",
    ),
    (
        "stratus",
        FIXTURES / "stratus_run" / "0704_1458_service_port_conflict_hotel_reservation_stratus_agent_trajectory.jsonl",
    ),
]


@pytest.mark.parametrize(("agent", "session_file"), SESSION_CASES)
def test_detect_and_convert_real_session_files(agent: str, session_file: Path):
    assert detect_agent(session_file) == agent
    trajectory = convert(session_file)
    assert isinstance(trajectory, Trajectory)
    assert trajectory.agent.name == agent
    Trajectory.model_validate(trajectory.to_json_dict())


@pytest.mark.parametrize(("agent", "session_file"), SESSION_CASES)
def test_explicit_agent_override(agent: str, session_file: Path):
    assert convert(session_file, agent=agent).agent.name == agent


def test_detects_legacy_gemini_json(tmp_path: Path):
    session_file = tmp_path / "session-legacy.json"
    session_file.write_text(
        json.dumps(
            {
                "sessionId": "legacy-gemini",
                "messages": [
                    {"type": "user", "content": "Investigate"},
                    {
                        "type": "gemini",
                        "content": "Done",
                        "model": "gemini-test",
                        "tokens": {"input": 2, "output": 1},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert detect_agent(session_file) == "gemini"
    assert convert(session_file).session_id == "legacy-gemini"


def test_public_failure_contract(tmp_path: Path):
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(FileNotFoundError):
        convert(missing)
    with pytest.raises(IsADirectoryError):
        convert(tmp_path)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError, match="empty"):
        convert(empty)

    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text("not json\n{also not json", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError, match="no recognizable JSON"):
        convert(malformed)

    invalid_utf8 = tmp_path / "invalid-utf8.jsonl"
    invalid_utf8.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(UnsupportedFormatError, match="not readable UTF-8 JSON"):
        detect_agent(invalid_utf8)
    with pytest.raises(UnsupportedFormatError, match="not readable UTF-8 JSON"):
        convert(invalid_utf8)

    unknown = tmp_path / "unknown.json"
    unknown.write_text('{"hello": "world"}', encoding="utf-8")
    with pytest.raises(UnsupportedFormatError, match="could not detect"):
        convert(unknown)

    with pytest.raises(UnsupportedAgentError, match="unsupported agent"):
        convert(unknown, agent="other")


def test_generic_message_record_is_not_misidentified_as_copilot(tmp_path: Path):
    session_file = tmp_path / "generic.jsonl"
    session_file.write_text('{"type":"message","content":"hello"}\n', encoding="utf-8")

    with pytest.raises(UnsupportedFormatError, match="could not detect"):
        detect_agent(session_file)


def test_jsonl_detection_does_not_read_the_entire_file():
    codex_file = SESSION_CASES[1][1]

    with patch.object(Path, "read_text", side_effect=AssertionError("unexpected full-file read")):
        assert detect_agent(codex_file) == "codex"


def test_recognized_but_unconvertible_file_raises(tmp_path: Path):
    session_file = tmp_path / "codex.jsonl"
    session_file.write_text('{"type":"session_meta","payload":{}}\n', encoding="utf-8")
    assert detect_agent(session_file) == "codex"
    with pytest.raises(ConversionFailedError, match="produced no trajectory"):
        convert(session_file)


def test_deliberately_wrong_agent_raises_conversion_error():
    claude_file = SESSION_CASES[0][1]
    with pytest.raises(ConversionFailedError):
        convert(claude_file, agent="codex")


def test_standalone_package_has_no_sregym_imports():
    package_root = Path(__file__).resolve().parents[2] / "atif_converter"
    offenders: list[str] = []
    for source_file in package_root.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (node.module == "sregym" or node.module.startswith("sregym."))
                or isinstance(node, ast.Import)
                and any(alias.name == "sregym" or alias.name.startswith("sregym.") for alias in node.names)
            ):
                offenders.append(f"{source_file}:{node.lineno}")
    assert offenders == []


def test_standalone_package_uses_relative_internal_imports():
    package_root = Path(__file__).resolve().parents[2] / "atif_converter"
    offenders: list[str] = []
    for source_file in package_root.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
                and node.module.startswith("atif_converter")
                or isinstance(node, ast.Import)
                and any(alias.name.startswith("atif_converter") for alias in node.names)
            ):
                offenders.append(f"{source_file}:{node.lineno}")
    assert offenders == []


def test_copied_folder_imports_and_converts_without_importing_sregym(tmp_path: Path):
    source_root = Path(__file__).resolve().parents[2] / "atif_converter"
    shutil.copytree(source_root, tmp_path / "atif_converter")
    session_file = tmp_path / "session.jsonl"
    shutil.copy2(SESSION_CASES[1][1], session_file)

    script = """
import sys
from atif_converter import convert

trajectory = convert("session.jsonl")
assert trajectory.agent.name == "codex"
assert not any(name == "sregym" or name.startswith("sregym.") for name in sys.modules)
print(trajectory.schema_version)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ATIF-v1.7"
