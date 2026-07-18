"""Edge-case coverage for the Codex session-JSONL adapter.

These build tiny synthetic session logs (a few JSONL lines each) to exercise
branches the real-run fixture may not hit: the false-positive submission
substring, the real submission envelope, orphan function_call_output, unknown
event types, and empty sessions dir.
"""

import json
from pathlib import Path

from sregym.traces import convert
from sregym.traces.adapters import codex
from sregym.traces.atif import Trajectory


def _write_session(run_dir: Path, records: list[dict]) -> None:
    """Materialize ``records`` as a Codex session log under ``run_dir/sessions/``."""
    sessions = run_dir / "sessions" / "rollout-20260101000000"
    sessions.mkdir(parents=True, exist_ok=True)
    with (sessions / "session.jsonl").open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _session_meta(session_id: str = "sess-1", cli_version: str = "0.1.0") -> dict:
    return {
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "cli_version": cli_version,
            "originator": "codex",
        },
    }


def _turn_context(model: str = "gpt-5") -> dict:
    return {
        "type": "turn_context",
        "payload": {"model": model, "turn_id": "turn-1"},
    }


def _turn_started(turn_id: str = "turn-1") -> dict:
    return {"type": "event_msg", "payload": {"type": "turn_started", "turn_id": turn_id}}


def _reasoning(text: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": text}],
        },
    }


def _assistant_message(text: str, *, ts: str = "2026-01-01T00:00:01Z") -> dict:
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _user_message(text: str, *, ts: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _function_call(call_id: str, name: str, arguments: dict, *, ts: str = "2026-01-01T00:00:02Z") -> dict:
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _function_call_output(call_id: str, output: str, *, ts: str = "2026-01-01T00:00:03Z") -> dict:
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


def _token_count(
    *,
    prompt: int = 100,
    completion: int = 10,
    cached: int = 0,
    total: int = 110,
    ts: str = "2026-01-01T00:00:04Z",
) -> dict:
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "cached_input_tokens": cached,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                },
                "total_token_usage": {
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "cached_input_tokens": cached,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                },
            },
        },
    }


def _canonical_run_dir(tmp_path: Path) -> Path:
    return tmp_path / "results" / "b" / "codex" / "p_hotel_reservation" / "run_1"


def test_no_sessions_returns_none(tmp_path):
    """Without a sessions/ dir, the adapter returns None gracefully."""
    run_dir = _canonical_run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    assert codex.to_atif(run_dir) is None


def test_empty_sessions_dir_returns_none(tmp_path):
    """sessions/ dir exists but has no .jsonl files -> None."""
    run_dir = _canonical_run_dir(tmp_path)
    (run_dir / "sessions").mkdir(parents=True)
    assert codex.to_atif(run_dir) is None


def test_unknown_event_types_are_skipped_gracefully(tmp_path):
    """Unknown event types should be silently skipped, not raise."""
    run_dir = _canonical_run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [
            _session_meta(),
            _turn_context(),
            _turn_started(),
            _reasoning("Plan the fix."),
            _assistant_message("Running a command."),
            _function_call("call_1", "shell", {"command": "echo hi"}),
            _function_call_output("call_1", "hi"),
            _token_count(),
            # An unknown event type should not break conversion.
            {"type": "some_future_event", "payload": {"foo": "bar"}},
            _assistant_message("Done."),
            _token_count(prompt=150, completion=5, total=155),
        ],
    )
    traj = codex.to_atif(run_dir)
    assert isinstance(traj, Trajectory)
    assert traj.agent.name == "codex"
    agent_steps = [s for s in traj.steps if s.source == "agent"]
    assert len(agent_steps) == 2


def test_orphan_function_call_output_becomes_tool_step(tmp_path):
    """A function_call_output without a matching function_call still converts."""
    run_dir = _canonical_run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [
            _session_meta(),
            _turn_context(),
            _turn_started(),
            _assistant_message("Inspecting."),
            _function_call_output("orphan_1", "stale output"),
            _token_count(),
        ],
    )
    traj = codex.to_atif(run_dir)
    assert isinstance(traj, Trajectory)
    # The orphan output creates its own tool_call step within the same API call group.
    tool_steps = [s for s in traj.steps if s.tool_calls]
    assert len(tool_steps) >= 1
    assert tool_steps[0].tool_calls[0].tool_call_id == "orphan_1"


def test_real_submission_envelope_is_detected_as_boundary(tmp_path):
    """The conductor submit-response envelope in tool output marks the boundary."""
    run_dir = _canonical_run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [
            _session_meta(),
            _turn_context(),
            _turn_started(),
            _reasoning("Diagnosing."),
            _assistant_message("Submitting diagnosis."),
            _function_call(
                "call_1",
                "shell",
                {"command": "curl -sS -X POST http://host/submit -d '{}'"},
            ),
            _function_call_output(
                "call_1",
                '{"status":"200","message":"Submission received"}',
            ),
            _token_count(),
        ],
    )
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    sregym = traj.extra["sregym"]
    # The diagnosis_submitted_step should be set (step containing the submit output).
    assert "diagnosis_submitted_step" in sregym
    boundary = sregym["diagnosis_submitted_step"]
    step = next(s for s in traj.steps if s.step_id == boundary)
    assert step.observation is not None
    blob = json.dumps([r.content for r in step.observation.results], default=str)
    assert "Submission received" in blob


def test_submission_substring_in_tool_output_is_not_a_boundary(tmp_path):
    """A bare phrase 'Submission received' without the JSON envelope is ignored."""
    run_dir = _canonical_run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [
            _session_meta(),
            _turn_context(),
            _turn_started(),
            _assistant_message("Checking old logs."),
            _function_call("call_1", "shell", {"command": "grep x log"}),
            _function_call_output("call_1", "old log: Submission received earlier"),
            _token_count(),
        ],
    )
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    sregym = traj.extra["sregym"]
    # No structured envelope -> no boundary detected.
    assert "diagnosis_submitted_step" not in sregym


def test_round_trip_through_model_validate(tmp_path):
    """The trajectory produced must round-trip through Trajectory.model_validate."""
    run_dir = _canonical_run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [
            _session_meta(),
            _turn_context(),
            _turn_started(),
            _reasoning("Plan."),
            _assistant_message("Running a command."),
            _function_call("call_1", "shell", {"command": "pwd"}),
            _function_call_output("call_1", "/workspace"),
            _token_count(),
        ],
    )
    traj = codex.to_atif(run_dir, sregym_meta={"problem_id": "p", "run": 1})
    assert isinstance(traj, Trajectory)
    # Round-trip: serialize -> validate.
    data = traj.to_json_dict()
    assert json.dumps(data)  # must be JSON-serializable
    Trajectory.model_validate(data)


def test_missing_model_name_does_not_crash(tmp_path):
    """If no turn_context with model is present, model_name is None."""
    run_dir = _canonical_run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [
            _session_meta(),
            _turn_started(),
            _assistant_message("Hello."),
            _token_count(),
        ],
    )
    traj = codex.to_atif(run_dir)
    assert isinstance(traj, Trajectory)
    assert traj.agent.model_name is None
