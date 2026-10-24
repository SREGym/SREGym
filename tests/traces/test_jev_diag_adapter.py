"""Jev's shared event format must export without losing its decision-agent identity."""

import json
import os
import shutil
from pathlib import Path

import pytest

from atif_converter import Trajectory, convert
from sregym.traces.convert import convert_run
from sregym.traces.postprocess import write_trajectory

FIXTURE = next((Path(__file__).parent / "fixtures" / "jev_diag_run" / "trajectory").glob("*.jsonl"))
PROBLEM = "service_port_conflict_hotel_reservation"


def _run(tmp_path, location="trajectory"):
    run = tmp_path / "results" / "batch" / "jev_diag" / PROBLEM / "run_1"
    native = run / location / FIXTURE.name
    native.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, native)
    return run, native


def test_jev_identity_model_usage_and_decisions_survive_conversion():
    trajectory = convert(FIXTURE)
    assert trajectory.agent.name == "jev_diag"
    assert trajectory.agent.model_name == "jev-test"
    assert len(trajectory.steps) == 6  # cumulative snapshots are not counted twice
    assert trajectory.final_metrics.total_prompt_tokens == 30
    assert trajectory.final_metrics.total_completion_tokens == 10
    calls = [step for step in trajectory.steps if step.source == "agent"]
    assert [step.llm_call_count for step in calls] == [0, 1, 1, 0]
    assert all(step.metrics is None for step in calls if step.llm_call_count == 0)
    assert calls[1].model_name == "jev-test"
    assert calls[1].tool_calls[0].arguments["state"] == {"ready": False}
    answer = json.loads(calls[1].observation.results[0].content)
    assert answer["component"]["probabilities"] == {"frontend": 0.8, "search": 0.2}
    assert trajectory.extra["jev_diag"]["submitted"] is True
    Trajectory.model_validate(trajectory.to_json_dict())


def test_jev_model_without_usage_does_not_invent_token_counts(tmp_path):
    _, native = _run(tmp_path)
    records = [json.loads(line) for line in native.read_text().splitlines()]
    for record in records:
        for message in record.get("messages", []):
            message.pop("usage_metadata", None)
    native.write_text("".join(json.dumps(record) + "\n" for record in records))
    trajectory = convert(native)
    assert trajectory.agent.model_name == "jev-test"
    assert trajectory.final_metrics.total_prompt_tokens is None
    assert trajectory.final_metrics.total_completion_tokens is None
    assert sum(step.llm_call_count or 0 for step in trajectory.steps) == 2


@pytest.mark.parametrize("location", ["trajectory", "."])
def test_jev_run_dispatch_and_postprocessing(tmp_path, location):
    run, _ = _run(tmp_path, location)
    output = write_trajectory(run)
    assert output == run / "trajectory.json"
    original = output.read_bytes()
    trajectory = Trajectory.model_validate_json(original)
    assert trajectory.agent.name == "jev_diag"
    assert "jev_diag" not in trajectory.extra
    assert "stratus" not in trajectory.extra
    meta = trajectory.extra["sregym"]
    assert meta["problem_id"] == PROBLEM
    assert meta["application"] == "Hotel Reservation"
    assert meta["results_path"] == f"batch/jev_diag/{PROBLEM}/run_1"
    assert meta["submitted"] is True
    assert meta["diagnosis_submitted_step"] == 6
    assert [stage["stage"] for stage in meta["stages"]] == ["diagnosis"]
    assert write_trajectory(run).read_bytes() == original


def test_jev_incomplete_run_is_not_marked_submitted(tmp_path):
    run, native = _run(tmp_path)
    records = [json.loads(line) for line in native.read_text().splitlines()][:3]
    native.write_text("".join(json.dumps(record) + "\n" for record in records))
    # Driver success means it ran, not that the conductor accepted a submission.
    (run / "jev_diag_run_results.json").write_text(json.dumps({"success": True}))
    trajectory = convert_run(run)
    assert trajectory.extra["sregym"]["submitted"] is False
    assert "diagnosis_submitted_step" not in trajectory.extra["sregym"]


@pytest.mark.parametrize("content", [None, "", "not json\n", '{"type":"metadata","agent":"jev_diag"}\n'])
def test_missing_or_unconvertible_jev_trace_is_non_fatal(tmp_path, content):
    run, native = _run(tmp_path)
    if content is None:
        native.unlink()
    else:
        native.write_text(content)
    assert write_trajectory(run) is None


def test_jev_uses_newest_trace_only(tmp_path):
    run, native = _run(tmp_path)
    old = run / "old_jev_diag_agent_trajectory.jsonl"
    shutil.copyfile(native, old)
    os.utime(old, ns=(1, 1))
    native.write_text(native.read_text().replace("jev-test", "jev-current"))
    assert convert_run(run).agent.model_name == "jev-current"
