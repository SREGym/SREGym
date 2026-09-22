"""Tests for the CloudThinker session -> ATIF conversion."""

import json
import shutil
from pathlib import Path

from atif_converter import Trajectory, detect_agent
from atif_converter import convert as convert_session
from sregym.traces import convert

FIXTURE = Path(__file__).parent / "fixtures" / "cloudthinker_run" / "cloudthinker_session.json"


def _canonical_run_dir(tmp_path: Path, tool: str = "cloudthinker_rca") -> Path:
    """Materialize the fixture under a canonical results/ path layout."""
    run_dir = tmp_path / "results" / "0917_1648" / tool / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.mkdir(parents=True)
    shutil.copy(FIXTURE, run_dir / "cloudthinker_session.json")
    return run_dir


def _session() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_detect_agent_reads_the_session_schema():
    assert detect_agent(FIXTURE) == "cloudthinker"


def test_convert_file_builds_sequential_steps():
    traj = convert_session(FIXTURE, agent="cloudthinker")
    assert isinstance(traj, Trajectory)
    assert traj.agent.name == "cloudthinker"
    assert traj.session_id == "service_port_conflict_hotel_reservation"
    assert [s.step_id for s in traj.steps] == [1, 2, 3, 4]
    assert [s.source for s in traj.steps] == ["user", "agent", "user", "agent"]
    assert traj.final_metrics.total_steps == 4
    # Validates (round-trips through the model).
    Trajectory.model_validate(traj.to_json_dict())


def test_agent_step_carries_reasoning_and_attached_tool_results():
    traj = convert_session(FIXTURE, agent="cloudthinker")
    step = traj.steps[1]
    assert step.reasoning_content
    assert [call.function_name for call in step.tool_calls] == [
        "computer_cli_read",
        "computer_cli_read",
    ]
    assert isinstance(step.tool_calls[0].arguments, dict)
    assert step.tool_calls[0].arguments
    results = step.observation.results
    assert len(results) == 2
    assert results[0].source_call_id == step.tool_calls[0].tool_call_id
    assert results[0].content
    assert results[0].extra["tool_name"] == "computer_cli_read"


def test_convert_run_dispatches_the_lane_name(tmp_path):
    traj = convert.convert_run(_canonical_run_dir(tmp_path))
    assert traj is not None
    assert traj.agent.name == "cloudthinker"
    assert traj.trajectory_id.endswith("cloudthinker_rca/service_port_conflict_hotel_reservation/run_1")

    sregym = traj.extra["sregym"]
    assert sregym["application"] == "Hotel Reservation"
    stages = sregym["stages"]
    assert [stage["stage"] for stage in stages] == ["diagnosis", "mitigation"]
    # The mitigation turn resumes the diagnosis conversation, so the split comes
    # from the driver's row watermark and not from a second conversation.
    assert stages[0]["conversation_id"] == stages[1]["conversation_id"]
    assert [stages[0]["first_step"], stages[0]["last_step"]] == [1, 2]
    assert [stages[1]["first_step"], stages[1]["last_step"]] == [3, 4]
    assert sregym["diagnosis_submitted_step"] == 2
    assert sregym["selection"] == "model:bedrock/global.anthropic.claude-opus-5"


def test_single_stage_session_has_no_diagnosis_boundary(tmp_path):
    session = _session()
    session["stages"] = session["stages"][:1]
    session["conversations"][0]["records"] = session["conversations"][0]["records"][:5]
    run_dir = _canonical_run_dir(tmp_path)
    (run_dir / "cloudthinker_session.json").write_text(json.dumps(session), encoding="utf-8")

    traj = convert.convert_run(run_dir)
    assert traj is not None
    assert len(traj.steps) == 2
    assert "diagnosis_submitted_step" not in traj.extra["sregym"]


def test_missing_or_empty_session_is_non_fatal(tmp_path):
    run_dir = tmp_path / "results" / "b" / "cloudthinker_rca" / "problem" / "run_1"
    run_dir.mkdir(parents=True)
    assert convert.convert_run(run_dir) is None

    (run_dir / "cloudthinker_session.json").write_text(
        json.dumps({"schema": "cloudthinker_session/v1", "stages": [], "conversations": []}),
        encoding="utf-8",
    )
    assert convert.convert_run(run_dir) is None


def test_unreadable_session_is_non_fatal(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    (run_dir / "cloudthinker_session.json").write_text("{not json", encoding="utf-8")
    assert convert.convert_run(run_dir) is None
