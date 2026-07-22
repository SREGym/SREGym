"""Edge-case coverage for the OpenCode adapter (session JSON path)."""

import json
from pathlib import Path

from atif_converter import Trajectory
from atif_converter.adapters import opencode
from sregym.traces import convert


def _canonical_run_dir(tmp_path: Path) -> Path:
    return tmp_path / "results" / "b" / "opencode" / "p_hotel_reservation" / "run_1"


def _write_session(
    run_dir: Path,
    *,
    session_id: str = "s1",
    version: str = "1.0.0",
    model_id: str = "m",
    messages: list[dict],
    tokens: dict | None = None,
    cost: float = 0,
) -> Path:
    """Write a synthetic session JSON under <run_dir>/sessions/."""
    sessions_dir = run_dir / "sessions" / "2026" / "06" / "30"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "info": {
            "id": session_id,
            "version": version,
            "model": {"id": model_id},
            "tokens": tokens or {"input": 0, "output": 0},
            "cost": cost,
        },
        "messages": messages,
    }
    path = sessions_dir / f"session-{session_id}.json"
    path.write_text(json.dumps(session))
    return path


def _session_file(run_dir: Path) -> Path:
    candidates = sorted((run_dir / "sessions").rglob("session-*.json"))
    return candidates[0] if candidates else run_dir / "sessions" / "session-missing.json"


def _user_msg(text: str, ts: int = 1700000000000) -> dict:
    return {"info": {"role": "user", "time": {"created": ts}}, "parts": [{"type": "text", "text": text}]}


def _assistant_msg(parts: list[dict], ts: int = 1700000001000) -> dict:
    return {"info": {"role": "assistant", "time": {"created": ts}}, "parts": parts}


def _text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def _reasoning_part(text: str) -> dict:
    return {"type": "reasoning", "text": text}


def _tool_part(call_id: str, name: str, tool_input: dict, output: str) -> dict:
    return {
        "type": "tool",
        "callID": call_id,
        "tool": name,
        "state": {"status": "completed", "input": tool_input, "output": output},
    }


def _finish_part(cost: float = 0.01, input_tok: int = 100, output_tok: int = 50) -> dict:
    return {
        "type": "step-finish",
        "cost": cost,
        "tokens": {"input": input_tok, "output": output_tok, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    }


def test_no_sessions_dir_returns_none(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    assert opencode.convert_file(_session_file(run_dir)) is None


def test_empty_sessions_dir_returns_none(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    (run_dir / "sessions").mkdir(parents=True)
    assert opencode.convert_file(_session_file(run_dir)) is None


def test_malformed_session_json_returns_none(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    sessions_dir = run_dir / "sessions" / "2026" / "06" / "30"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "session-bad.json").write_text("not json at all")
    assert opencode.convert_file(_session_file(run_dir)) is None


def test_no_messages_returns_none(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(run_dir, messages=[])
    assert opencode.convert_file(_session_file(run_dir)) is None


def test_unknown_part_types_skipped(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(
        run_dir,
        messages=[
            _assistant_msg(
                [
                    {"type": "some_future_part", "data": "ignored"},
                    _text_part("Hello"),
                    _finish_part(),
                ]
            ),
        ],
    )
    traj = opencode.convert_file(_session_file(run_dir))
    assert isinstance(traj, Trajectory)
    assert len(traj.steps) == 1
    assert traj.steps[0].message == "Hello"


def test_text_only_turn(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(
        run_dir,
        messages=[
            _assistant_msg([_text_part("Hello."), _finish_part(cost=0.015, input_tok=100, output_tok=50)]),
        ],
        tokens={"input": 100, "output": 50},
        cost=0.015,
    )
    traj = opencode.convert_file(_session_file(run_dir))
    assert isinstance(traj, Trajectory)
    assert traj.steps[0].message == "Hello."
    assert traj.steps[0].metrics.prompt_tokens == 100
    assert traj.steps[0].metrics.completion_tokens == 50


def test_tool_call_turn(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(
        run_dir,
        messages=[
            _assistant_msg(
                [
                    _text_part("Creating file."),
                    _tool_part("call_1", "write", {"path": "/x"}, "Done."),
                    _finish_part(),
                ]
            ),
        ],
    )
    traj = opencode.convert_file(_session_file(run_dir))
    assert isinstance(traj, Trajectory)
    step = traj.steps[0]
    assert step.tool_calls[0].function_name == "write"
    assert step.observation.results[0].content == "Done."


def test_reasoning_content_captured(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(
        run_dir,
        messages=[
            _assistant_msg([_reasoning_part("Plan first."), _text_part("On it."), _finish_part()]),
        ],
    )
    traj = opencode.convert_file(_session_file(run_dir))
    assert traj.steps[0].reasoning_content == "Plan first."


def test_user_step_from_user_message(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(
        run_dir,
        messages=[
            _user_msg("Fix the bug."),
            _assistant_msg([_text_part("Done."), _finish_part()]),
        ],
    )
    traj = opencode.convert_file(_session_file(run_dir))
    assert traj.steps[0].source == "user"
    assert traj.steps[0].message == "Fix the bug."
    assert traj.steps[1].source == "agent"


def test_submission_envelope_detected_as_boundary(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(
        run_dir,
        messages=[
            _assistant_msg(
                [
                    _text_part("Submitting."),
                    _tool_part("c1", "bash", {"cmd": "curl"}, '{"status":"200","message":"Submission received"}'),
                    _finish_part(),
                ]
            ),
        ],
    )
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    assert "diagnosis_submitted_step" in traj.extra["sregym"]


def test_submission_substring_not_a_boundary(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(
        run_dir,
        messages=[
            _assistant_msg(
                [
                    _tool_part("c1", "bash", {"cmd": "grep"}, "old log: Submission received"),
                    _finish_part(),
                ]
            ),
        ],
    )
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    assert "diagnosis_submitted_step" not in traj.extra["sregym"]


def test_final_metrics_from_info_tokens(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(
        run_dir,
        messages=[_assistant_msg([_text_part("ok"), _finish_part()])],
        tokens={"input": 500, "output": 50, "reasoning": 5, "cache": {"read": 200, "write": 10}},
        cost=0.02,
    )
    traj = opencode.convert_file(_session_file(run_dir))
    fm = traj.final_metrics
    assert fm.total_prompt_tokens == 700  # input + cache_read
    assert fm.total_completion_tokens == 50
    assert fm.total_cached_tokens == 200
    assert fm.total_cost_usd == 0.02
    assert fm.extra["input_tokens"] == 500
    assert fm.extra["reasoning_tokens"] == 5


def test_round_trip_through_model_validate(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    _write_session(
        run_dir,
        messages=[
            _assistant_msg(
                [
                    _reasoning_part("Planning."),
                    _text_part("Running."),
                    _tool_part("c1", "bash", {"cmd": "ls"}, "file.txt"),
                    _finish_part(cost=0.01, input_tok=10, output_tok=5),
                ]
            ),
        ],
    )
    traj = opencode.convert_file(_session_file(run_dir))
    assert isinstance(traj, Trajectory)
    Trajectory.model_validate(traj.to_json_dict())
