"""Tests for the conversion dispatch (path parsing, app mapping, boundary)."""

import json
import shutil
from pathlib import Path

import pytest

from sregym.traces import convert
from sregym.traces.atif import Trajectory

FIXTURE_RUN = Path(__file__).parent / "fixtures" / "claudecode_run"


def _canonical_run_dir(tmp_path: Path) -> Path:
    """Materialize the fixture under a canonical results/ path layout."""
    run_dir = tmp_path / "results" / "0629_1125" / "claudecode" / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.parent.mkdir(parents=True)
    shutil.copytree(FIXTURE_RUN, run_dir)
    return run_dir


def test_convert_run_dispatches_claudecode(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    assert traj.agent.name == "claudecode"
    # Validates (round-trips through the model).
    Trajectory.model_validate(traj.to_json_dict())


def test_extra_sregym_populated(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["problem_id"] == "service_port_conflict_hotel_reservation"
    assert sregym["application"] == "Hotel Reservation"
    assert sregym["run"] == 1
    assert sregym["submitted"] is True
    assert sregym["results_path"].endswith("claudecode/service_port_conflict_hotel_reservation/run_1")


# The fixture submits the diagnosis at step 8 (the first step whose observation
# carries the conductor's {"status":"200","message":"Submission received"}
# envelope; a later step 13 submits the mitigation). Hardcoded so this test
# pins the value independently of the detection algorithm it exercises.
EXPECTED_DIAGNOSIS_STEP = 8


def test_diagnosis_submitted_step(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["diagnosis_submitted_step"] == EXPECTED_DIAGNOSIS_STEP
    # Sanity: that step really does carry the submission envelope.
    step = next(s for s in traj.steps if s.step_id == EXPECTED_DIAGNOSIS_STEP)
    assert step.observation is not None
    blob = json.dumps([r.content for r in step.observation.results], default=str)
    assert "Submission received" in blob


@pytest.mark.parametrize(
    "problem_id,expected",
    [
        ("service_port_conflict_hotel_reservation", "Hotel Reservation"),
        ("duplicate_pvc_mounts_social_network", "Social Network"),
        ("missing_env_variable_astronomy_shop", "Astronomy Shop"),
        ("kubelet_crash", None),
        ("operator_overload_replicas", None),
    ],
)
def test_application_longest_suffix_mapping(problem_id, expected):
    assert convert.map_application(problem_id) == expected


def test_parse_run_path():
    p = Path("/x/results/0629_1125/claudecode/service_port_conflict_hotel_reservation/run_3")
    info = convert.parse_run_path(p)
    assert info.tool == "claudecode"
    assert info.problem_id == "service_port_conflict_hotel_reservation"
    assert info.run == 3
    assert info.batch == "0629_1125"


# --------------------------------------------------------------------------- #
# Codex dispatch
# --------------------------------------------------------------------------- #


def _codex_session_meta(session_id: str = "codex-sess-1") -> dict:
    return {
        "type": "session_meta",
        "payload": {"id": session_id, "cli_version": "0.1.0"},
    }


def _codex_turn_context(model: str = "gpt-5") -> dict:
    return {"type": "turn_context", "payload": {"model": model, "turn_id": "turn-1"}}


def _codex_turn_started() -> dict:
    return {"type": "event_msg", "payload": {"type": "turn_started", "turn_id": "turn-1"}}


def _codex_reasoning(text: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": text}]},
    }


def _codex_assistant(text: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:01Z",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _codex_fn_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:02Z",
        "payload": {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)},
    }


def _codex_fn_output(call_id: str, output: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:03Z",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def _codex_token_count(prompt: int, completion: int, total: int) -> dict:
    return {
        "type": "event_msg",
        "timestamp": "2026-01-01T00:00:04Z",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "cached_input_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                },
                "total_token_usage": {
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "cached_input_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                },
            },
        },
    }


def _materialize_codex_run(tmp_path: Path) -> Path:
    """Create a synthetic codex run under a canonical results/ path."""
    run_dir = tmp_path / "results" / "0629_1959" / "codex" / "service_port_conflict_hotel_reservation" / "run_1"
    sessions = run_dir / "sessions" / "rollout-20260101000000"
    sessions.mkdir(parents=True)
    events = [
        _codex_session_meta(),
        _codex_turn_context(),
        _codex_turn_started(),
        _codex_reasoning("Diagnosing the issue."),
        _codex_assistant("Submitting diagnosis."),
        _codex_fn_call("call_1", "shell", {"command": "curl -sS -X POST http://host/submit"}),
        _codex_fn_output("call_1", '{"status":"200","message":"Submission received"}'),
        _codex_token_count(100, 20, 120),
    ]
    with (sessions / "session.jsonl").open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    # Results JSON for submitted detection.
    results_path = run_dir / "codex_results_service_port_conflict_hotel_reservation_20260629_140556.json"
    results_path.write_text(json.dumps({"success": True}), encoding="utf-8")
    return run_dir


def test_convert_run_dispatches_codex(tmp_path):
    run_dir = _materialize_codex_run(tmp_path)
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    assert traj.agent.name == "codex"
    Trajectory.model_validate(traj.to_json_dict())


def test_codex_extra_sregym_populated(tmp_path):
    run_dir = _materialize_codex_run(tmp_path)
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["problem_id"] == "service_port_conflict_hotel_reservation"
    assert sregym["application"] == "Hotel Reservation"
    assert sregym["run"] == 1
    assert sregym["submitted"] is True
    assert sregym["results_path"].endswith("codex/service_port_conflict_hotel_reservation/run_1")
    # Diagnosis boundary should be detected (the step with the submit output).
    assert "diagnosis_submitted_step" in sregym


# --------------------------------------------------------------------------- #
# OpenCode dispatch
# --------------------------------------------------------------------------- #


OPCODEX_FIXTURE = Path(__file__).parent / "fixtures" / "opencode_run"


def test_convert_run_dispatches_opencode(tmp_path):
    run_dir = tmp_path / "results" / "b" / "opencode" / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.parent.mkdir(parents=True)
    shutil.copytree(OPCODEX_FIXTURE, run_dir)
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    assert traj.agent.name == "opencode"
    Trajectory.model_validate(traj.to_json_dict())


def test_opencode_extra_sregym_populated(tmp_path):
    run_dir = tmp_path / "results" / "b" / "opencode" / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.parent.mkdir(parents=True)
    shutil.copytree(OPCODEX_FIXTURE, run_dir)
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["problem_id"] == "service_port_conflict_hotel_reservation"
    assert sregym["application"] == "Hotel Reservation"
    assert sregym["run"] == 1
    assert sregym["submitted"] is True
    assert "diagnosis_submitted_step" in sregym


# --------------------------------------------------------------------------- #
# Copilot dispatch
# --------------------------------------------------------------------------- #


COPILOT_FLAT_FIXTURE = Path(__file__).parent / "fixtures" / "copilot_run_flat"


def _materialize_copilot_run(tmp_path: Path) -> Path:
    """Create a copilot run under a canonical results/ path, with a submit marker."""
    run_dir = tmp_path / "results" / "b" / "copilot" / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.mkdir(parents=True)
    shutil.copy(
        COPILOT_FLAT_FIXTURE / "copilot-cli.jsonl",
        run_dir / "copilot-cli.jsonl",
    )
    # Append a tool_use + tool_result carrying the conductor submit response so
    # the submitted flag + diagnosis boundary are exercised.
    with open(run_dir / "copilot-cli.jsonl", "a") as f:
        f.write(
            json.dumps(
                {
                    "type": "tool_use",
                    "id": "sub1",
                    "name": "bash",
                    "input": {"command": "curl -X POST http://host/submit"},
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_use_id": "sub1",
                    "content": '{"status":"200","message":"Submission received"}',
                }
            )
            + "\n"
        )
    # A copilot results JSON like the client writes.
    results_path = run_dir / "copilot_results_service_port_conflict_hotel_reservation_20260704_131952.json"
    results_path.write_text(json.dumps({"success": True}))
    return run_dir


def test_convert_run_dispatches_copilot(tmp_path):
    run_dir = _materialize_copilot_run(tmp_path)
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    assert traj.agent.name == "copilot"
    Trajectory.model_validate(traj.to_json_dict())


def test_copilot_extra_sregym_populated(tmp_path):
    run_dir = _materialize_copilot_run(tmp_path)
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["problem_id"] == "service_port_conflict_hotel_reservation"
    assert sregym["application"] == "Hotel Reservation"
    assert sregym["run"] == 1
    assert sregym["results_path"].endswith("copilot/service_port_conflict_hotel_reservation/run_1")
    # Submission marker in the tool result -> submitted True + boundary detected.
    assert sregym["submitted"] is True
    assert "diagnosis_submitted_step" in sregym


# --------------------------------------------------------------------------- #
# Stratus dispatch
# --------------------------------------------------------------------------- #


STRATUS_FIXTURE = Path(__file__).parent / "fixtures" / "stratus_run"


def test_convert_run_dispatches_stratus(tmp_path):
    run_dir = tmp_path / "results" / "b" / "stratus" / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.parent.mkdir(parents=True)
    shutil.copytree(STRATUS_FIXTURE, run_dir)
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    assert traj.agent.name == "stratus"
    Trajectory.model_validate(traj.to_json_dict())


def test_stratus_extra_sregym_populated(tmp_path):
    run_dir = tmp_path / "results" / "b" / "stratus" / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.parent.mkdir(parents=True)
    shutil.copytree(STRATUS_FIXTURE, run_dir)
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["problem_id"] == "service_port_conflict_hotel_reservation"
    assert sregym["application"] == "Hotel Reservation"
    assert sregym["run"] == 1
    # Adapter-populated keys survive convert.py's merge.
    assert [s["stage"] for s in sregym["stages"]][0] == "diagnosis"
    assert sregym["submitted"] is True
    # Stage-derived boundary (Stratus's submit marker differs from the generic one).
    assert sregym["diagnosis_submitted_step"] > 0


# --------------------------------------------------------------------------- #
# Gemini dispatch
# --------------------------------------------------------------------------- #


GEMINI_FIXTURE = Path(__file__).parent / "fixtures" / "gemini_run"


def test_convert_run_dispatches_gemini(tmp_path):
    run_dir = tmp_path / "results" / "b" / "gemini" / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.parent.mkdir(parents=True)
    shutil.copytree(GEMINI_FIXTURE, run_dir)
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    assert traj.agent.name == "gemini"
    Trajectory.model_validate(traj.to_json_dict())


def test_gemini_extra_sregym_populated(tmp_path):
    run_dir = tmp_path / "results" / "b" / "gemini" / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.parent.mkdir(parents=True)
    shutil.copytree(GEMINI_FIXTURE, run_dir)
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["problem_id"] == "service_port_conflict_hotel_reservation"
    assert sregym["application"] == "Hotel Reservation"
    assert sregym["run"] == 1
    assert sregym["results_path"].endswith("gemini/service_port_conflict_hotel_reservation/run_1")
