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


@pytest.mark.parametrize("first_failed", [False, True])
def test_repeated_questions_keep_each_response_usage_and_tool_identity(tmp_path, first_failed):
    from clients.jev_diag.trace import DecisionTrace

    trace = DecisionTrace(tmp_path / "trace.jsonl", problem_id=PROBLEM)
    for visit, tokens, answer in ((1, 10, "first"), (2, 30, "second")):
        trace.record("jev", "request", label="investigate:cache", state={"visit": visit}, questions={"q": {}})
        if visit == 1 and first_failed:
            trace.record("jev", "response", label="investigate:cache", error="API unavailable")
        else:
            trace.record(
                "jev",
                "response",
                label="investigate:cache",
                model="test",
                usage={"input_tokens": tokens, "output_tokens": visit},
                answers={"q": {"choice": answer}},
            )
    trace.close("ok")
    trajectory = convert(trace.write_trajectory(tmp_path / "trajectory"))
    calls = [step for step in trajectory.steps if step.tool_calls]
    assert len(calls) == 2
    assert calls[0].tool_calls[0].tool_call_id != calls[1].tool_calls[0].tool_call_id
    assert [step.tool_calls[0].arguments["state"]["visit"] for step in calls] == [1, 2]
    outputs = [step.observation.results[0].content for step in calls]
    if first_failed:
        assert outputs[0] == "API unavailable"
    else:
        assert json.loads(outputs[0])["q"]["choice"] == "first"
    assert json.loads(outputs[1])["q"]["choice"] == "second"
    assert trajectory.final_metrics.total_prompt_tokens == (30 if first_failed else 40)
    assert trajectory.final_metrics.total_completion_tokens == (2 if first_failed else 3)


@pytest.mark.parametrize("fail_after_triage", [False, True])
def test_fallback_diagnosis_and_submission_survive_export(tmp_path, fail_after_triage):
    from types import SimpleNamespace

    from typesafe_sdk import ChoiceAnswer

    from clients.jev_diag.trace import DecisionTrace
    from clients.jev_diag.v1_1.collector import ClusterSnapshot
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser

    cid = "deployment/shop/frontend"

    class Unavailable:
        calls = 0

        def system_one(self, state, questions):
            self.calls += 1
            if self.calls == 1 and fail_after_triage:
                answer = ChoiceAnswer(choice=cid, confidence=1.0, probabilities={cid: 1.0, "other": 0.0})
                return SimpleNamespace(
                    model="test",
                    usage=None,
                    answers={"root_cause_component": answer},
                    choices={"root_cause_component": answer},
                )
            raise RuntimeError("API unavailable")

    snapshot = ClusterSnapshot(
        app={"name": "shop", "namespaces": ["shop"]},
        components={
            cid: {
                "kind": "Deployment",
                "name": "frontend",
                "namespace": "shop",
                "signals": ["no pods exist"],
                "healthy": False,
            }
        },
        cluster={},
    )
    trace = DecisionTrace(tmp_path / "trace.jsonl", problem_id=PROBLEM)
    diagnosis = IterativeDiagnoser(Unavailable(), fetch_detail=lambda *args: {}, trace=trace).diagnose(snapshot)
    trace.record("submit", "response", response={"success": True})
    trace.close("ok")
    recorded = [r for r in trace.records if (r["phase"], r["event"]) == ("decide", "diagnosis_text")]
    assert len(recorded) == 1 and recorded[0]["text"] == diagnosis.text
    trajectory = convert(trace.write_trajectory(tmp_path / "trajectory"))
    submission = next(
        step for step in trajectory.steps if any(call.function_name == "submit" for call in step.tool_calls or [])
    )
    assert submission.message == diagnosis.text
    assert submission.tool_calls[0].arguments["ans"] == diagnosis.text
    assert trajectory.extra["jev_diag"]["submitted"] is True
