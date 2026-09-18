import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).parent / "integration" / "validate_lite.py"
    spec = importlib.util.spec_from_file_location("lite_suite_runner_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_non_kind_context_is_rejected(monkeypatch, runner):
    monkeypatch.setattr(runner.subprocess, "check_output", Mock(return_value="production\n"))
    with pytest.raises(RuntimeError, match="disposable local KIND"):
        runner.cluster_identity()


@pytest.mark.parametrize("local_nodes,provider", [("other-node", "kind://docker/kind/node"), ("node", "cloud://node")])
def test_context_name_alone_does_not_authorize_cluster_mutations(monkeypatch, runner, local_nodes, provider):
    node = {"metadata": {"name": "node"}, "spec": {"providerID": provider}}
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        Mock(side_effect=["kind-kind\n", json.dumps({"items": [node]}), local_nodes]),
    )
    with pytest.raises(RuntimeError, match="(does not match|not from the expected)"):
        runner.cluster_identity()


def _setup(monkeypatch, runner, tmp_path, *, resume=False):
    problems = runner.SREGYM_LITE_PROBLEMS[:2]
    argv = ["validate_lite.py", "--output-dir", str(tmp_path), "--problems", *problems]
    if resume:
        argv.append("--resume")
    monkeypatch.setattr(runner.sys, "argv", argv)
    identity = {"context": "kind-test", "nodes": [{"name": "node", "uid": "uid-1", "architecture": "arm64"}]}
    monkeypatch.setattr(runner, "cluster_identity", lambda: identity)
    return problems, identity


def test_success_writes_each_result_and_unbuffered_child_logs(monkeypatch, runner, tmp_path):
    problems, _ = _setup(monkeypatch, runner, tmp_path)

    def launch(command, **kwargs):
        assert command[1] == "-u"
        result_path = Path(command[command.index("--json-summary") + 1])
        result_path.write_text(json.dumps({"passed": True}))
        return Mock(returncode=0)

    popen = Mock(side_effect=launch)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    assert runner.main() == 0
    report = json.loads((tmp_path / "suite.json").read_text())
    assert [attempt["problem"] for attempt in report["attempts"]] == list(problems)
    assert all(attempt["passed"] for attempt in report["attempts"])


def test_failed_child_stops_before_next_problem(monkeypatch, runner, tmp_path):
    _setup(monkeypatch, runner, tmp_path)
    popen = Mock(return_value=Mock(returncode=1))
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    assert runner.main() == 1
    popen.assert_called_once()
    attempt = json.loads((tmp_path / "suite.json").read_text())["attempts"][0]
    assert not attempt["passed"]
    assert not attempt["timed_out"]


def test_timeout_terminates_child_process_group_and_records_failure(monkeypatch, runner, tmp_path):
    _setup(monkeypatch, runner, tmp_path)
    process = Mock(pid=12345, returncode=-15)
    process.wait.side_effect = [runner.subprocess.TimeoutExpired("child", 1800), -15]
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    killpg = Mock()
    monkeypatch.setattr(runner.os, "killpg", killpg)
    assert runner.main() == 1
    killpg.assert_called_once_with(12345, runner.signal.SIGTERM)
    attempt = json.loads((tmp_path / "suite.json").read_text())["attempts"][0]
    assert attempt["timed_out"]
    assert not attempt["passed"]


def test_resume_skips_passed_problems(monkeypatch, runner, tmp_path):
    problems, identity = _setup(monkeypatch, runner, tmp_path, resume=True)
    (tmp_path / "suite.json").write_text(
        json.dumps(
            {
                "cluster": identity,
                "profile": "full",
                "with_loki": True,
                "attempts": [{"problem": problem, "passed": True} for problem in problems],
            }
        )
    )
    popen = Mock()
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    assert runner.main() == 0
    popen.assert_not_called()


def test_resume_rejects_recreated_cluster(monkeypatch, runner, tmp_path):
    _, identity = _setup(monkeypatch, runner, tmp_path, resume=True)
    old_identity = {**identity, "nodes": [{**identity["nodes"][0], "uid": "old-uid"}]}
    (tmp_path / "suite.json").write_text(
        json.dumps({"cluster": old_identity, "profile": "full", "with_loki": True, "attempts": []})
    )
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
