import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from sregym.conductor import conductor_api
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.oracles import usage_reporting as oracles
from sregym.conductor.oracles.llm_as_a_judge.judge import DiagnosisJudge
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.usage_reporting import UsageReporting
from sregym.conductor.submission import SubmissionAttemptClosed
from sregym.run_artifacts import RunArtifacts


@pytest.fixture
def problem(tmp_path, monkeypatch):
    monkeypatch.delenv("SREGYM_USAGE_DIAGNOSIS_KEY", raising=False)
    bundle = tmp_path / "bundle"
    key = bundle / "grading/diagnosis/P2.json"
    key.parent.mkdir(parents=True)
    key.write_text(
        json.dumps(
            {
                "schema": "usage-diagnosis-v1",
                "saved_state": "P2",
                "root_cause": {"component": "reporting-control", "description": "private-evidence"},
            }
        )
    )
    problem = UsageReporting(world_root=bundle, runs_dir=tmp_path / "runs")
    problem.app.run_dir.mkdir(parents=True)
    problem.app.wait_frozen = Mock(return_value=problem.app.run_dir / "end-state")
    problem.app.retain_workspace = Mock()
    problem.app.cleanup = Mock()
    return problem


@pytest.mark.parametrize("first_dimension_yes,score,passed", [(2, 0.67, False), (3, 0.78, True)])
def test_standard_diagnosis_judge_and_checklist(problem, monkeypatch, first_dimension_yes, score, passed):
    oracle = problem.diagnosis_oracle
    assert type(oracle) is LLMAsAJudgeOracle
    assert type(oracle.judge) is DiagnosisJudge
    assert oracle.expected == problem.root_cause
    assert problem.root_cause == ("[fault_spec] component=reporting-control; namespace=company || private-evidence")
    judge = oracle.judge
    assert judge._threshold == 0.70
    assert judge._dimension_ids == ["D1", "D2", "D3"]
    judge._backend = SimpleNamespace(model_name="fixture")
    answers = [
        {"id": q["id"], "answer": "Yes" if i < (first_dimension_yes if d == 0 else 2) else "No"}
        for d, dimension in enumerate(judge._config["dimensions"])
        for i, q in enumerate(dimension["questions"])
    ]
    monkeypatch.setattr(judge, "_call_llm_with_retry", lambda _prompt: answers)
    result = oracle.evaluate("Submitted evidence")
    assert result["composite_score"] == score
    assert result["success"] is passed
    assert "layers" not in result


def test_single_root_cause_loads_from_external_bundle(problem, tmp_path, monkeypatch):
    key = tmp_path / "truth.json"
    key.write_text(
        json.dumps(
            {
                "schema": "usage-diagnosis-v1",
                "saved_state": "P2",
                "root_cause": {"component": "reporting-control", "description": "external-evidence"},
            }
        )
    )
    monkeypatch.setenv("SREGYM_USAGE_DIAGNOSIS_KEY", str(key))
    configured = UsageReporting(world_root=problem.app.project)
    assert configured.root_cause.endswith(" || external-evidence")
    key.write_text(json.dumps({"schema": "usage-diagnosis-v1", "saved_state": "other"}))
    with pytest.raises(ValueError, match="match the selected saved state"):
        UsageReporting(world_root=problem.app.project)
    key.write_text(
        json.dumps(
            {
                "schema": "usage-diagnosis-v1",
                "saved_state": "P2",
                "root_cause": {"component": "reporting-control", "description": ""},
            }
        )
    )
    with pytest.raises(ValueError, match="one root cause"):
        UsageReporting(world_root=problem.app.project)
    key.unlink()
    with pytest.raises(FileNotFoundError):
        UsageReporting(world_root=problem.app.project)


def install_grader(problem, monkeypatch, status="pass", failure=None, schema="sregym-grade-v1", mitigation=None):
    app = problem.app
    output = app.grader.parent / "results" / ("sregym-" + app.run_dir.name)

    def grade(command, **kwargs):
        app.wait_frozen.assert_called_once()
        assert command[command.index("--capture") + 1] == str(app.run_dir / "end-state")
        assert "--no-recurrence" not in command
        assert kwargs["timeout"] == 930
        if failure:
            raise failure
        output.mkdir(parents=True)
        (output / "GRADE.json").write_text(
            json.dumps(
                {
                    "schema": schema,
                    "run": str(app.run_dir),
                    "mitigation": mitigation or {"status": status},
                    "validity": {"status": "ungraded"},
                    "limitations": ["Missing native evidence"],
                }
            )
        )

    command = Mock(side_effect=grade)
    monkeypatch.setattr(oracles.subprocess, "run", command)
    return command


@pytest.mark.parametrize("status,success", [("pass", True), ("fail", False)])
def test_grader_pass_fail_mapping_is_boolean_and_idempotent(problem, monkeypatch, status, success):
    command = install_grader(problem, monkeypatch, status)
    result = problem.mitigation_oracle.evaluate()
    assert result["success"] is success
    assert "mitigation" not in result
    assert Path(result["grade_path"]).is_file()
    assert problem.mitigation_oracle.evaluate() is result
    command.assert_called_once()


def test_large_grader_tables_do_not_block_normal_artifact_publication(problem, monkeypatch, tmp_path):
    install_grader(
        problem,
        monkeypatch,
        mitigation={"status": "pass", "custody": {"status": "pass", "buckets": ["x" * 200_000]}},
    )
    result = problem.mitigation_oracle.evaluate()
    assert result["success"] is True
    assert Path(result["grade_path"]).stat().st_size > 200_000
    assert len(json.dumps(result)) < 16_384
    run = RunArtifacts.create(
        staging_root=tmp_path / "staging",
        results_root=tmp_path / "results",
        problem_id="usage_reporting",
        agent="codex",
        attempt=1,
    )
    snapshot = {"problem_id": run.problem_id, **{f"Mitigation.{k}": v for k, v in result.items()}}
    published = run.finalize_and_publish(snapshot=snapshot, fieldnames=list(snapshot))
    assert (published / "usage_reporting_results.csv").is_file()


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("grade", 930), subprocess.CalledProcessError(1, "grade")]
)
def test_grader_errors_are_harness_failures(problem, monkeypatch, failure):
    install_grader(problem, monkeypatch, failure=failure)
    result = problem.mitigation_oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "world_grader_failed"
    assert result["failure_class"] == "harness_error"


@pytest.mark.parametrize("status", ["ungraded", "unknown", None])
def test_incomplete_grader_result_is_a_harness_failure(problem, monkeypatch, status):
    install_grader(problem, monkeypatch, status)
    result = problem.mitigation_oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "world_grader_failed"
    assert result["failure_class"] == "harness_error"


def test_unknown_schema_is_not_a_pass(problem, monkeypatch):
    install_grader(problem, monkeypatch, "pass", schema="future-v2")
    result = problem.mitigation_oracle.evaluate()
    assert result["reason"] == "world_grader_failed"


def test_exit_without_mitigation_submission_still_records_frozen_outcome(problem, monkeypatch):
    conductor = Conductor(ConductorConfig(manage_kubernetes=False, stages=("diagnosis", "mitigation")))
    conductor.problem_id = "usage_reporting"
    monkeypatch.setattr(conductor.problems, "get_problem_instance", lambda _name: problem)
    monkeypatch.setattr(conductor, "dependency_check", Mock())
    deadline = time.monotonic() + 1800
    problem.app.deploy = Mock(side_effect=lambda: setattr(problem.app, "agent_deadline_monotonic", deadline))
    install_grader(problem, monkeypatch)

    async def run():
        await conductor.start_problem()
        conductor.close_submissions()
        conductor.finish_problem_in_background()
        await conductor.wait_for_submission_work(timeout=3)

    asyncio.run(run())
    assert problem.mitigation_oracle.result["success"] is True
    assert "Mitigation" not in conductor.results
    assert conductor.finalize_attempt_status() == "incomplete"
    assert conductor.missing_submission_stages() == ["diagnosis", "mitigation"]


def test_real_conductor_deploy_submit_diagnosis_mitigation_oracle_cleanup(problem, monkeypatch):
    """The actual API and stage machinery, with only the world and judge replaced."""
    conductor = Conductor(ConductorConfig(manage_kubernetes=False, stages=("diagnosis", "mitigation")))
    conductor.problem_id = "usage_reporting"
    monkeypatch.setattr(conductor.problems, "get_problem_instance", lambda _name: problem)
    monkeypatch.setattr(conductor, "dependency_check", Mock())
    monkeypatch.setattr(
        problem.diagnosis_oracle, "evaluate", Mock(return_value={"success": False, "composite_score": 0.0})
    )
    deadline = time.monotonic() + 1800
    problem.app.deploy = Mock(side_effect=lambda: setattr(problem.app, "agent_deadline_monotonic", deadline))
    problem.app.agent_deadline_monotonic = None
    install_grader(problem, monkeypatch)
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    async def run():
        await conductor.start_problem()
        assert conductor.submission_stage == "diagnosis"
        assert conductor.agent_deadline_monotonic == deadline
        transport = httpx.ASGITransport(app=conductor_api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            info = (await client.get("/get_app")).json()
            assert info["stages"] == ["diagnosis", "mitigation"]
            assert "operator_instructions" in info
            assert "grading" not in info
            assert "private-evidence" not in json.dumps(info)
            assert (
                await client.post("/submit", json={"stage": "diagnosis", "solution": "diagnosis"})
            ).status_code == 200
            await conductor.wait_for_submission_work(timeout=3)
            assert conductor.submission_stage == "mitigation"
            assert (await client.post("/submit", json={"stage": "mitigation", "solution": ""})).status_code == 200
            await conductor.wait_for_submission_work(timeout=3)
        assert conductor.submission_stage == "done"
        assert conductor.results["Mitigation"]["success"] is True
        assert conductor.missing_submission_stages() == []

    asyncio.run(run())
    problem.app.deploy.assert_called_once()
    assert problem.app.cleanup.call_count == 2  # leftovers, then completed attempt


def test_conductor_rejects_submission_at_deadline_before_driver_poll():
    conductor = Conductor(ConductorConfig(manage_kubernetes=False))
    conductor.agent_deadline_monotonic = time.monotonic() - 0.001
    conductor._accepting_submissions = True
    with pytest.raises(SubmissionAttemptClosed, match="response deadline has passed"):
        asyncio.run(conductor.submit("late answer"))
