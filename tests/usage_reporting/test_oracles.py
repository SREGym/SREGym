import asyncio
import json
import os
import subprocess
import sys
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
from sregym.conductor.problems import usage_reporting as usage_problem
from sregym.conductor.problems.usage_reporting import UsageReporting
from sregym.run_artifacts import RunArtifacts


def write_key(keys, description, saved_state="usage-reporting"):
    keys.mkdir(parents=True, exist_ok=True)
    cause = {"component": "reporting-control", "description": description}
    (keys / "usage-reporting.json").write_text(
        json.dumps({"schema": "usage-diagnosis-v1", "saved_state": saved_state, "root_cause": cause})
    )


@pytest.fixture
def problem(tmp_path, monkeypatch):
    write_key(tmp_path / "keys", "private-evidence")
    monkeypatch.setattr(usage_problem, "KEYS", tmp_path / "keys")
    problem = UsageReporting(world_root=tmp_path / "bundle", runs_dir=tmp_path / "runs")
    problem.app.run_dir.mkdir(parents=True)
    problem.app.agent_workspace = tmp_path / "workspace"
    problem.app.freeze = Mock(return_value=problem.app.run_dir / "end-state")
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


def test_root_cause_key_must_match_the_saved_state_and_name_one_cause(problem, tmp_path):
    keys = tmp_path / "keys"
    write_key(keys, "private-evidence", saved_state="other")
    with pytest.raises(ValueError, match="match the selected saved state"):
        UsageReporting(world_root=problem.app.project)
    write_key(keys, "")
    with pytest.raises(ValueError, match="one root cause"):
        UsageReporting(world_root=problem.app.project)
    (keys / "usage-reporting.json").unlink()
    with pytest.raises(FileNotFoundError):
        UsageReporting(world_root=problem.app.project)


def install_grader(problem, monkeypatch, status="pass", failure=None, schema="sregym-grade-v1", mitigation=None):
    app = problem.app
    output = app.grader.parent / "results" / ("sregym-" + app.run_dir.name)

    def grade(command, **kwargs):
        app.freeze.assert_called_once()
        assert command[command.index("--capture") + 1] == str(app.run_dir / "end-state")
        assert kwargs["timeout"] == 1860
        assert command[command.index("--runtime-limit") + 1] == "1800"
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
    monkeypatch.setattr(oracles, "run_grader", command)
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
    problem.app.freeze.assert_called_once()
    problem.app.retain_workspace.assert_called_once_with(problem.app.agent_workspace)


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
    "failure", [subprocess.TimeoutExpired("grade", 1860), subprocess.CalledProcessError(1, "grade")]
)
def test_grader_errors_are_harness_failures(problem, monkeypatch, failure):
    install_grader(problem, monkeypatch, failure=failure)
    result = problem.mitigation_oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "world_grader_failed"
    assert result["failure_class"] == "harness_error"


def test_aborted_run_is_a_harness_failure_without_grading(problem, monkeypatch):
    command = install_grader(problem, monkeypatch, "fail")
    (problem.app.run_dir / "abort.json").write_text(json.dumps({"reason": "Host resource limit: swap"}))
    result = problem.mitigation_oracle.evaluate()
    assert result["reason"] == "world_run_aborted"
    assert result["failure_class"] == "harness_error"
    command.assert_not_called()
    problem.app.retain_workspace.assert_called_once()


def test_failed_freeze_is_a_harness_failure_without_grading(problem):
    problem.app.freeze.side_effect = subprocess.CalledProcessError(1, "freeze")

    result = problem.mitigation_oracle.evaluate()

    assert result["reason"] == "world_freeze_failed"
    assert result["failure_class"] == "harness_error"
    assert not (problem.app.run_dir / "grader.log").exists()


def test_failed_workspace_retention_is_a_harness_failure_without_grading(problem):
    problem.app.retain_workspace.side_effect = OSError(28, "No space left on device")

    result = problem.mitigation_oracle.evaluate()

    assert result["reason"] == "world_workspace_retention_failed"
    assert result["failure_class"] == "harness_error"
    assert "No space left on device" in result["detail"]["error"]
    assert not (problem.app.run_dir / "grader.log").exists()


def test_grader_timeout_stops_its_whole_process_group(tmp_path):
    marker = tmp_path / "child.pid"
    script = "import subprocess,sys,time; p=subprocess.Popen(['sleep','60']); open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)"
    with (tmp_path / "grader.log").open("w") as log, pytest.raises(subprocess.TimeoutExpired):
        oracles.run_grader([sys.executable, "-c", script, str(marker)], cwd=tmp_path, log=log, timeout=2)
    child = int(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):  # the child can exit while /proc is read
            break
        if state == "Z":
            break
        time.sleep(0.05)
    else:
        os.kill(child, 9)
        pytest.fail("Grader child survived the timeout")


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


def test_real_conductor_deploy_submit_diagnosis_mitigation_oracle_cleanup(problem, monkeypatch):
    """The actual API and stage machinery, with only the world and judge replaced."""
    conductor = Conductor(ConductorConfig(uses_cluster=False, stages=("diagnosis", "mitigation")))
    conductor.problem_id = "usage_reporting"
    monkeypatch.setattr(conductor.problems, "get_problem_instance", lambda _name: problem)
    monkeypatch.setattr(conductor, "dependency_check", Mock())
    monkeypatch.setattr(
        problem.diagnosis_oracle, "evaluate", Mock(return_value={"success": False, "composite_score": 0.0})
    )
    problem.app.deploy = Mock()
    install_grader(problem, monkeypatch)
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    async def run():
        await conductor.start_problem()
        assert conductor.submission_stage == "diagnosis"
        transport = httpx.ASGITransport(app=conductor_api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            info = (await client.get("/get_app")).json()
            assert info["stages"] == ["diagnosis", "mitigation"]
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


def test_default_key_is_the_shipped_key_for_the_default_state(tmp_path):
    problem = UsageReporting(world_root=tmp_path / "bundle", runs_dir=tmp_path / "runs")
    assert problem.app.state == "usage-reporting"
    assert "fleet contract" in problem.root_cause
