import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from sregym.agent_launcher import AgentLauncher, AgentProcess
from sregym.run_artifacts import ArtifactFinalizationError, RunArtifacts
from sregym.service.container_runner import ContainerRunner
from sregym.service.internet_policy import InternetPolicy


def record(host="blocked.test"):
    return {
        "timestamp": "2026-09-06T12:00:00+00:00",
        "method": "GET",
        "scheme": "https",
        "host": host,
        "port": 443,
        "reason": "endpoint-not-allowed",
    }


@pytest.fixture
def audit_runner(tmp_path):
    launcher = AgentLauncher()
    runner = ContainerRunner()
    runner._egress._tmp_dir = tmp_path / "proxy"
    runner._egress._tmp_dir.mkdir()
    launcher._container_runner = runner
    log = runner._egress._tmp_dir / "blocked-requests.jsonl"
    log.touch()
    return launcher, runner, log


def append(log, value):
    with log.open("a") as handle:
        handle.write(json.dumps(value) + "\n")


def new_run(tmp_path, attempt=1):
    return RunArtifacts.create(
        staging_root=tmp_path / "staging",
        results_root=tmp_path / "results",
        problem_id="test_fault",
        agent="codex",
        attempt=attempt,
    )


def test_attempts_exclude_preflight_and_previous_requests(audit_runner):
    launcher, runner, log = audit_runner
    append(log, record("preflight.test"))
    for host in ("first.test", "second.test"):
        process = AgentProcess("codex", Mock())
        process.egress_blocked_start = runner.blocked_request_count()
        append(log, record(host))
        result = launcher.internet_policy_result(process)
        assert result["blocked_requests"] == 1
        assert result["blocked_request_details"] == [record(host)]


@pytest.mark.parametrize("status", ["complete", "incomplete"])
def test_published_audit_survives_proxy_cleanup(audit_runner, tmp_path, status):
    launcher, _, log = audit_runner
    append(log, record())
    process = AgentProcess("codex", Mock(returncode=1 if status == "incomplete" else 0))
    launcher._procs[process.name] = process
    run = new_run(tmp_path)
    launcher.cleanup_agent(process.name)
    audit = launcher.internet_policy_result(process)
    run.save_network_audit(audit)
    assert run.network_audit_path.parent == run.active_dir.parent
    assert not run.network_audit_path.is_relative_to(run.active_dir)
    launcher.cleanup_all()
    assert not log.exists()
    snapshot = {"problem_id": run.problem_id, "run_status": status, "blocked_requests": audit["blocked_requests"]}
    destination = run.finalize_and_publish(snapshot=snapshot, fieldnames=list(snapshot))
    assert json.loads((destination / "internet_audit.json").read_text()) == audit
    assert not run.network_audit_path.exists()


def test_old_logs_are_sanitized_on_export(audit_runner):
    launcher, _, log = audit_runner
    append(
        log, {**record(), "path": "/secret/token?key=secret", "headers": {"Authorization": "secret"}, "body": "secret"}
    )
    result = launcher.internet_policy_result(AgentProcess("codex", Mock()))
    assert result["blocked_request_details"] == [record()]
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["open", "filtered"])
def test_empty_or_unstarted_agent_has_explicit_empty_audit(audit_runner, mode):
    launcher, _, _ = audit_runner
    launcher._internet_policy = InternetPolicy.from_mode(mode, agent_name="codex")
    for process in (None, AgentProcess("codex", Mock())):
        assert launcher.internet_policy_result(process) == {
            "internet_access": mode,
            "blocked_requests": 0,
            "blocked_request_details": [],
        }


@pytest.mark.parametrize("bad_record", ["{truncated", "{}", "[]", "null"])
def test_bad_audit_is_not_reported_as_zero_denials(audit_runner, bad_record):
    launcher, _, log = audit_runner
    log.write_text(bad_record + "\n")
    result = launcher.internet_policy_result(AgentProcess("codex", Mock()))
    assert "internet_audit_error" in result
    assert "blocked_requests" not in result
    assert "blocked_request_details" not in result


def test_audit_read_failure_is_explicit(audit_runner, monkeypatch):
    launcher, runner, _ = audit_runner
    monkeypatch.setattr(runner, "blocked_request_records", Mock(side_effect=PermissionError("sensitive path")))
    result = launcher.internet_policy_result(AgentProcess("codex", Mock()))
    assert "internet_audit_error" in result
    assert "sensitive" not in json.dumps(result)
    assert "blocked_requests" not in result


def test_missing_proxy_log_is_an_audit_error(audit_runner):
    launcher, _, log = audit_runner
    log.unlink()
    result = launcher.internet_policy_result(AgentProcess("codex", Mock()))
    assert "internet_audit_error" in result
    assert "blocked_requests" not in result


def test_truncated_proxy_log_is_an_audit_error(audit_runner):
    launcher, _, _ = audit_runner
    process = AgentProcess("codex", Mock())
    process.egress_blocked_start = 1
    result = launcher.internet_policy_result(process)
    assert "internet_audit_error" in result
    assert "blocked_requests" not in result


def test_failed_artifact_validation_retains_separate_host_audit(tmp_path):
    run = new_run(tmp_path)
    audit = {"blocked_request_details": [record()]}
    run.save_network_audit(audit)
    (run.active_dir / "agent.json").write_text(json.dumps({"exposed": run.problem_id}))
    with pytest.raises(ArtifactFinalizationError, match="real problem id"):
        run.finalize_and_publish(snapshot={}, fieldnames=[])
    assert json.loads(run.network_audit_path.read_text()) == audit
    assert not run.final_dir.exists()


def test_publication_replaces_agent_symlink_without_writing_its_target(tmp_path):
    run = new_run(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("do not change")
    (run.active_dir / "internet_audit.json").symlink_to(outside)
    audit = {"blocked_request_details": [record()]}
    run.save_network_audit(audit)
    dest = run.finalize_and_publish(snapshot={"problem_id": run.problem_id}, fieldnames=["problem_id"])
    assert outside.read_text() == "do not change"
    assert not (dest / "internet_audit.json").is_symlink()
    assert json.loads((dest / "internet_audit.json").read_text()) == audit


def test_audit_name_collision_retains_host_copy(tmp_path):
    run = new_run(tmp_path)
    (run.active_dir / "internet_audit.json").mkdir()
    run.save_network_audit({"blocked_requests": 1})
    with pytest.raises(ArtifactFinalizationError):
        run.finalize_and_publish(snapshot={"problem_id": run.problem_id}, fieldnames=["problem_id"])
    assert run.network_audit_path.exists()


def test_corrupt_host_audit_is_a_publication_error(tmp_path):
    run = new_run(tmp_path)
    run.network_audit_path.write_text("{truncated")
    with pytest.raises(ArtifactFinalizationError):
        run.finalize_and_publish(snapshot={"problem_id": run.problem_id}, fieldnames=["problem_id"])
    assert run.network_audit_path.exists()
    assert not run.final_dir.exists()


def test_save_failure_does_not_leave_partial_json(tmp_path, monkeypatch):
    run = new_run(tmp_path)
    run.save_network_audit({"blocked_requests": 1})
    monkeypatch.setattr("sregym.run_artifacts.os.replace", Mock(side_effect=OSError("disk error")))
    with pytest.raises(OSError):
        run.save_network_audit({"blocked_requests": 2})
    assert json.loads(run.network_audit_path.read_text()) == {"blocked_requests": 1}
    assert not list(run.active_dir.parent.glob("*.tmp"))


@pytest.mark.parametrize("scenario", ["complete", "exit", "timeout", "cleanup_error", "artifact_error", "save_error"])
def test_driver_persists_audit_before_shutdown(audit_runner, tmp_path, monkeypatch, scenario):
    path = Path(__file__).resolve().parents[2] / "main.py"
    spec = importlib.util.spec_from_file_location("audit_driver_test", path)
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    launcher, runner, log = audit_runner
    append(log, record("preflight.test"))
    conductor = Mock()
    conductor.problems.get_problem_ids.return_value = ["test_fault"]
    conductor.get_agent_kubeconfig_path.return_value = None
    conductor.stage_sequence = ["Diagnosis", "Mitigation"]
    conductor.close_submissions.return_value = False
    conductor.missing_submission_stages.return_value = ["Mitigation"]
    conductor.wait_for_submission_work = AsyncMock()
    if scenario == "cleanup_error":
        conductor.wait_for_submission_work.side_effect = RuntimeError("cleanup unavailable")

    async def start_problem():
        conductor.results = {}
        conductor.submission_stage = "active" if scenario in ("exit", "timeout", "cleanup_error") else "done"
        return object()

    def incomplete(reason, **fields):
        conductor.results.update(run_status="incomplete", incomplete_reason=reason, **fields)

    def finalize():
        return conductor.results.setdefault("run_status", "complete")

    async def start_agent(_reg):
        process = AgentProcess("codex", Mock(returncode=1 if scenario == "exit" else 0))
        process.egress_blocked_start = runner.blocked_request_count()
        append(log, record(f"attempt-{process.egress_blocked_start}.test"))
        launcher._procs[process.name] = process
        if scenario == "artifact_error":
            (Path(os.environ["AGENT_LOGS_DIR"]) / "exposure.txt").write_text("test_fault")
        return process

    conductor.start_problem = start_problem
    conductor.record_incomplete_attempt.side_effect = incomplete
    conductor.finalize_attempt_status.side_effect = finalize
    monkeypatch.setattr(launcher, "ensure_started", start_agent)
    monkeypatch.setattr(main, "LAUNCHER", launcher)
    monkeypatch.setattr(main, "get_current_datetime_formatted", lambda: "audit-test")
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(main.trace_postprocess, "write_trajectory", Mock(return_value=None))
    monkeypatch.chdir(tmp_path)
    if scenario == "save_error":
        monkeypatch.setattr(RunArtifacts, "save_network_audit", Mock(side_effect=OSError("disk error")))

    if scenario == "cleanup_error":
        with pytest.raises(main.BenchmarkCampaignAborted) as failure:
            main.driver_loop(conductor, ["test_fault"], "codex", n_attempts=2)
        rows = failure.value.partial_results[0]["codex"]
        assert len(rows) == 1
    else:
        rows = main.driver_loop(
            conductor, ["test_fault"], "codex", n_attempts=2, agent_timeout=-1 if scenario == "timeout" else 60
        )[0]["codex"]
        assert len(rows) == 2
    launcher.cleanup_all()
    assert not log.exists()
    for row in rows:
        assert row["blocked_requests"] == 1
        assert "blocked_request_details" not in row
        if scenario == "save_error":
            assert row["internet_audit_error"] == "could not save blocked-request records"
            continue
        if scenario == "artifact_error":
            audit_path = Path(row["internet_audit_staging_path"])
            assert row["artifact_finalization_failed"]
        else:
            audit_path = (
                tmp_path / "results/audit-test/codex/test_fault" / f"run_{row['attempt']}" / "internet_audit.json"
            )
        audit = json.loads(audit_path.read_text())
        assert audit["blocked_requests"] == len(audit["blocked_request_details"]) == 1
        assert audit["blocked_request_details"][0]["host"] == f"attempt-{row['attempt']}.test"
