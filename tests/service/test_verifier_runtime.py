"""Verifier trust boundary, live state handoff and lifecycle regressions."""

import asyncio
import io
import json
import logging
import stat
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, messages_to_dict

from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.kubelet_eviction_threshold_misconfig_mitigation import (
    KubeletEvictionThresholdMisconfigMitigationOracle,
)
from sregym.conductor.oracles.llm_as_a_judge.judge import DiagnosisJudge, LLMJudge
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.workload.hotel_search import HotelSearchWorkload, WorkloadSnapshot
from sregym.service import verifier_runtime
from sregym.service.agent_visibility_policy import HIDDEN_NAMESPACES, VERIFIER_PROBE_NAMESPACE
from sregym.service.k8s_proxy import _is_hidden_namespace_request
from sregym.service.verifier_runtime import (
    VerifierError,
    VerifierRuntime,
    _json_frame,
    _resource_call,
    _source_files,
    verifier_connection,
)
from sregym.service.verifier_state import restore_oracle, save_oracle_snapshot, snapshot_oracle
from sregym.service.verifier_worker import RemoteWorkload, read_frame


class TestProblem(Problem):
    __test__ = False

    def __init__(self):
        super().__init__(SimpleNamespace(namespace="test-app"))

    def inject_fault(self):
        pass

    def recover_fault(self):
        pass


class StateOracle(Oracle):
    def __init__(self, problem):
        super().__init__(problem)
        self.baseline = {"frontend": 2}

    def evaluate(self):
        return {"success": self.baseline == {"frontend": 2}}


def test_snapshot_preserves_baseline_identity_and_compound_state(tmp_path):
    problem = TestProblem()
    first, second = StateOracle(problem), MitigationOracle(problem)
    second.replica_count = {"frontend": 2, "store": 3}
    problem.mitigation_oracle = CompoundedOracle(problem, first, second)
    # A diagnosis client containing unpicklable runtime state is not transported.
    problem.diagnosis_oracle = SimpleNamespace(thread=threading.Thread(), key="private-provider-key")
    payload, resources = snapshot_oracle(problem.mitigation_oracle, tmp_path)
    restored = restore_oracle(payload)
    assert not resources
    assert b"private-provider-key" not in payload
    assert restored.problem.mitigation_oracle is restored
    assert restored.problem.diagnosis_oracle is None
    children = list(restored.oracles.values())
    assert children[0].baseline == {"frontend": 2}
    assert children[1].replica_count == {"frontend": 2, "store": 3}
    assert children[0].problem is children[1].problem is restored.problem


def test_unknown_live_state_fails_instead_of_silently_dropping_it(tmp_path):
    problem = TestProblem()
    problem.active_thread = threading.Thread()
    with pytest.raises(TypeError, match="explicit verifier state adapter"):
        snapshot_oracle(StateOracle(problem), tmp_path)


def test_explicit_handle_exclusion_preserves_the_owners_thread(tmp_path):
    problem = TestProblem()
    problem.verifier_excluded_fields = ("active_thread",)
    problem.active_thread = threading.Thread()
    restored = restore_oracle(snapshot_oracle(StateOracle(problem), tmp_path)[0])
    assert restored.problem.active_thread is None
    assert problem.active_thread is not None


def test_locks_and_events_recreated_without_losing_aliases(tmp_path):
    problem = TestProblem()
    problem.lock = problem.same_lock = threading.Lock()
    problem.stop = threading.Event()
    problem.stop.set()
    restored = restore_oracle(snapshot_oracle(StateOracle(problem), tmp_path)[0]).problem
    assert restored.lock is restored.same_lock
    assert restored.lock is not problem.lock
    assert restored.stop.is_set()


def test_repo_paths_are_rebased_for_linux_container(tmp_path):
    problem = TestProblem()
    problem.path = tmp_path / "sregym/service/metadata/test.json"
    restored = restore_oracle(snapshot_oracle(StateOracle(problem), tmp_path)[0])
    assert restored.problem.path == Path("/opt/sregym/sregym/service/metadata/test.json")


def test_live_search_workload_stays_on_host_without_restarting_traffic(tmp_path):
    problem = TestProblem()
    workload = HotelSearchWorkload("test-app")
    problem.workload = workload
    problem.same_workload = workload
    payload, resources = snapshot_oracle(StateOracle(problem), tmp_path)
    assert resources == [workload]
    handle = object()
    restored = restore_oracle(payload, lambda index: handle if index == 0 else None)
    assert restored.problem.workload is restored.problem.same_workload is handle
    with pytest.raises(ValueError, match="Live host workloads"):
        save_oracle_snapshot(StateOracle(problem), tmp_path / "oracle.pickle")


@pytest.mark.parametrize("judge_type", [DiagnosisJudge, LLMJudge])
def test_diagnosis_snapshot_keeps_credentials_and_live_model_client_on_the_owner(tmp_path, judge_type):
    oracle = StateOracle(TestProblem())
    oracle.expected = "checkout uses port 8082 instead of 8080"
    oracle.judge = judge_type(api_key="private-judge-key", model_name="test-judge")
    backend = SimpleNamespace(
        api_key="private-backend-key",
        active_thread=threading.Thread(),
        inference=Mock(return_value=SimpleNamespace(content=[{"type": "text", "text": "raw model response"}])),
    )
    oracle.judge._backend = backend
    payload, resources = snapshot_oracle(oracle, tmp_path)
    assert resources == [("model", backend)]
    assert b"private-judge-key" not in payload
    assert b"private-backend-key" not in payload

    def call(index, operation, args):
        return _resource_call(resources, {"index": index, "op": operation, "args": args})

    restored = restore_oracle(payload, lambda index: RemoteWorkload(index, call))
    assert restored.expected == oracle.expected
    assert restored.judge.api_key is None
    assert restored.judge.model_name == "test-judge"
    assert not hasattr(restored.judge.backend, "api_key")
    assert not hasattr(restored.judge.backend, "active_thread")
    messages = [SystemMessage(content="private grading instructions"), HumanMessage(content="agent diagnosis")]
    assert restored.judge.backend.inference(messages).content == [{"type": "text", "text": "raw model response"}]
    backend.inference.assert_called_once_with(messages)
    assert oracle.judge.api_key == "private-judge-key"
    assert oracle.judge.backend is backend
    if isinstance(oracle.judge, DiagnosisJudge):
        assert restored.judge._config == oracle.judge._config
        assert restored.judge._threshold == oracle.judge._threshold


@pytest.mark.parametrize("operation,args", [("evaluate", []), ("metrics", []), ("model_inference", ["raw string"])])
def test_model_io_handle_cannot_invoke_grading_or_unrelated_host_operations(operation, args):
    backend = Mock()
    with pytest.raises(VerifierError, match="model operation"):
        _resource_call([("model", backend)], {"index": 0, "op": operation, "args": args})
    assert not backend.mock_calls


def test_model_io_returns_only_response_content():
    messages = [HumanMessage(content="diagnosis to judge")]
    backend = Mock()
    backend.inference.return_value = SimpleNamespace(content="raw response", api_key="private-response-metadata")
    result = _resource_call(
        [("model", backend)], {"index": 0, "op": "model_inference", "args": [messages_to_dict(messages)]}
    )
    assert result == "raw response"
    backend.inference.assert_called_once_with(messages)


def test_saved_snapshot_is_private_and_cannot_replace_a_previous_attempt(tmp_path):
    path = tmp_path / "oracle.pickle"
    oracle = StateOracle(TestProblem())
    save_oracle_snapshot(oracle, path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert restore_oracle(path.read_bytes()).baseline == oracle.baseline
    with pytest.raises(FileExistsError):
        save_oracle_snapshot(oracle, path)


def test_hardening_and_private_channel_have_no_host_mounts_or_grading_port():
    runtime = VerifierRuntime()
    runtime.image, runtime.network = "sha256:" + "a" * 64, "kind"
    args = runtime.docker_command("sregym-verifier-random")
    for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=10001:10001"):
        assert flag in args
    assert not any(
        arg in {"-v", "--volume", "--mount", "-p", "--publish", "--privileged", "--network=host"} for arg in args
    )
    assert not any("docker.sock" in arg or "workspace" in arg or "provider-key" in arg for arg in args)
    assert args[-1] == runtime.image


def test_build_context_excludes_untracked_credentials_and_linked_host_files(tmp_path, monkeypatch):
    service = tmp_path / "sregym/service"
    service.mkdir(parents=True)
    (service / "tracked.py").write_text("trusted source")
    (service / "credential.txt").write_text("untracked private token")
    (service / ".env").write_text("provider secret")
    (service / "verifier_worker.py").write_text("new verifier source")
    outside = tmp_path.parent / "outside-verifier-source"
    outside.mkdir(exist_ok=True)
    (outside / "host.py").write_text("private host data")
    (service / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        "sregym.service.verifier_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=b"sregym/service/tracked.py\0sregym/service/.env\0sregym/service/linked/host.py\0"
        ),
    )
    names = {path.relative_to(tmp_path).as_posix() for path in _source_files(tmp_path)}
    assert names == {"sregym/service/tracked.py", "sregym/service/verifier_worker.py"}


def test_verifier_image_matches_runner_python_and_does_not_reuse_another_version(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("same source for every interpreter")
    monkeypatch.setattr(verifier_runtime, "_source_files", lambda root: [source])
    images, builds = {}, []

    def docker(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            image = images.get(command[-1])
            return SimpleNamespace(returncode=0 if image else 1, stdout=image or "")
        assert command[:2] == ["docker", "build"]
        tag = command[command.index("-t") + 1]
        version = command[command.index("--build-arg") + 1]
        builds.append((version, kwargs["stdin"].read()))
        images[tag] = f"sha256:{len(builds):064x}"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(verifier_runtime.subprocess, "run", docker)

    def build_for_version(minor, micro):
        monkeypatch.setattr(
            verifier_runtime,
            "sys",
            SimpleNamespace(
                implementation=SimpleNamespace(name="cpython"),
                version_info=SimpleNamespace(major=3, minor=minor, micro=micro, releaselevel="final"),
            ),
        )
        return verifier_runtime.build_verifier_image(tmp_path)

    images_by_version = [build_for_version(12, 3), build_for_version(12, 4), build_for_version(13, 0)]
    assert build_for_version(12, 3) == images_by_version[0]
    assert len(set(images_by_version)) == 3
    assert [version for version, _ in builds] == [
        "PYTHON_VERSION=3.12.3",
        "PYTHON_VERSION=3.12.4",
        "PYTHON_VERSION=3.13.0",
    ]
    assert builds[0][1] == builds[1][1] == builds[2][1]


def test_verifier_rejects_an_interpreter_implementation_that_the_image_cannot_match(monkeypatch, tmp_path):
    monkeypatch.setattr(verifier_runtime, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pypy")))
    docker = Mock()
    monkeypatch.setattr(verifier_runtime.subprocess, "run", docker)
    with pytest.raises(VerifierError, match="CPython runner"):
        verifier_runtime.build_verifier_image(tmp_path)
    docker.assert_not_called()


@pytest.mark.parametrize("releaselevel", ["alpha", "beta", "candidate"])
def test_verifier_rejects_nonfinal_interpreters_before_building(monkeypatch, tmp_path, releaselevel):
    monkeypatch.setattr(
        verifier_runtime,
        "sys",
        SimpleNamespace(
            implementation=SimpleNamespace(name="cpython"), version_info=SimpleNamespace(releaselevel=releaselevel)
        ),
    )
    docker = Mock()
    monkeypatch.setattr(verifier_runtime.subprocess, "run", docker)
    with pytest.raises(VerifierError, match="final CPython release"):
        verifier_runtime.build_verifier_image(tmp_path)
    docker.assert_not_called()


@pytest.mark.parametrize("line", [b"{}", b"garbage\n", b"[]\n", b'{"success":NaN}\n', b'{"success":Infinity}\n'])
def test_malformed_protocol_never_becomes_a_verdict(line):
    with pytest.raises(VerifierError):
        _json_frame(line)


@pytest.mark.parametrize(
    "line",
    [
        b'{"run_id":"old","run_id":"current"}\n',
        b'{"result":{"success":false,"success":true}}\n',
        b'{"type":"resource","op":"metrics","op":"model_inference"}\n',
    ],
)
def test_duplicate_protocol_keys_cannot_replace_verdict_or_operation(line):
    with pytest.raises(VerifierError, match="Duplicate verifier JSON key"):
        _json_frame(line)


def test_worker_rejects_truncated_input():
    with pytest.raises(ValueError, match="protocol frame"):
        read_frame(io.BytesIO(b'{"run_id":"wrong"}'))


@pytest.mark.parametrize(
    "operation,args", [("evaluate", []), ("snapshot", [float("nan")]), ("set_rate", [-1]), ("start", [1])]
)
def test_live_io_adapter_does_not_allow_arbitrary_methods(operation, args):
    workload = Mock()
    with pytest.raises(VerifierError):
        _resource_call([workload], {"index": 0, "op": operation, "args": args})
    workload.evaluate.assert_not_called()


def test_live_io_adapter_preserves_workload_observations():
    snapshot = WorkloadSnapshot(10, 9, 8, 1.0, 8 / 9, 0.2)
    workload = SimpleNamespace(
        snapshot=lambda seconds: snapshot, metrics=SimpleNamespace(snapshot=lambda: {"requests": 10})
    )
    assert _resource_call([workload], {"index": 0, "op": "snapshot", "args": [10]})["completed"] == 9
    assert _resource_call([workload], {"index": 0, "op": "metrics", "args": []}) == {"requests": 10}


def test_materialized_external_kubeconfig_is_not_rewritten(monkeypatch):
    config = {
        "clusters": [{"cluster": {"server": "https://cluster.example:6443"}}],
        "users": [{"user": {"token": "private-token"}}],
    }
    monkeypatch.setattr(
        "sregym.service.verifier_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(config)),
    )
    actual, network = verifier_connection()
    assert actual == config
    assert network == "bridge"


def test_exec_plugin_is_materialized_on_host_without_forwarding_its_environment(monkeypatch):
    config = {
        "current-context": "test",
        "contexts": [{"name": "test", "context": {"cluster": "cluster", "user": "verifier"}}],
        "clusters": [{"name": "cluster", "cluster": {"server": "https://cluster.example"}}],
        "users": [{"name": "verifier", "user": {"exec": {"command": "credential-helper"}}}],
    }
    monkeypatch.setattr(
        "sregym.service.verifier_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(config)),
    )

    def materialize(self, target):
        target.api_key["authorization"] = "Bearer materialized-token"

    monkeypatch.setattr("kubernetes.config.kube_config.KubeConfigLoader.load_and_set", materialize)
    actual, _network = verifier_connection()
    assert actual["users"][0]["user"] == {"token": "materialized-token"}


def test_verifier_cannot_grade_through_an_unauthenticated_tls_endpoint(monkeypatch):
    config = {
        "clusters": [{"cluster": {"server": "https://cluster.example", "insecure-skip-tls-verify": True}}],
        "users": [{"user": {"token": "private-token"}}],
    }
    monkeypatch.setattr(
        "sregym.service.verifier_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(config)),
    )
    with pytest.raises(VerifierError, match="TLS certificate verification"):
        verifier_connection()


def test_cancel_before_start_never_launches_a_container(monkeypatch):
    runtime = VerifierRuntime()
    runtime.cancel()
    launch = Mock()
    monkeypatch.setattr("sregym.service.verifier_runtime.subprocess.Popen", launch)
    with pytest.raises(VerifierError, match="cancelled"):
        runtime.evaluate_snapshot(b"untrusted")
    launch.assert_not_called()


def test_pre_cancelled_runtime_cannot_be_prepared_again(monkeypatch):
    runtime = VerifierRuntime()
    runtime.cancel()
    build, connection = Mock(), Mock()
    monkeypatch.setattr(verifier_runtime, "build_verifier_image", build)
    monkeypatch.setattr(verifier_runtime, "verifier_connection", connection)
    with pytest.raises(VerifierError, match="cancelled"):
        runtime.prepare()
    build.assert_not_called()
    connection.assert_not_called()


@pytest.mark.parametrize("cancel_during", ["build", "connection"])
def test_cancellation_during_preparation_prevents_fault_injection(monkeypatch, cancel_during):
    runtime = VerifierRuntime()
    entered, release = threading.Event(), threading.Event()
    image = "sha256:" + "a" * 64

    def slow_operation(result):
        entered.set()
        assert release.wait(timeout=5), "Preparation was never released"
        return result

    build = Mock(side_effect=lambda _root: slow_operation(image) if cancel_during == "build" else image)
    connection = Mock(side_effect=lambda _path: slow_operation(({}, "bridge")))
    monkeypatch.setattr(verifier_runtime, "build_verifier_image", build)
    monkeypatch.setattr(verifier_runtime, "verifier_connection", connection)
    conductor = Conductor.__new__(Conductor)
    conductor.config = ConductorConfig()
    conductor.logger = logging.getLogger("test.verifier")
    conductor.stage_sequence = [{"name": "diagnosis"}]
    conductor.problem = SimpleNamespace(mitigation_oracle=Mock(), inject_fault=Mock())
    conductor._verifier_runtime = runtime
    outcomes = []

    def inject():
        try:
            conductor._inject_fault()
        except Exception as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=inject, daemon=True)
    thread.start()
    try:
        assert entered.wait(timeout=2), "Preparation never started"
        runtime.cancel()
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], VerifierError)
    assert "cancelled" in str(outcomes[0])
    conductor.problem.mitigation_oracle.capture_baseline.assert_not_called()
    conductor.problem.inject_fault.assert_not_called()
    assert runtime._cancelled.is_set()
    if cancel_during == "build":
        connection.assert_not_called()


@pytest.mark.parametrize("pending_cleanup", [False, True])
def test_new_attempt_gets_a_fresh_runtime_without_reviving_the_cancelled_owner(pending_cleanup):
    previous = VerifierRuntime()
    previous.cancel()
    conductor = Conductor.__new__(Conductor)
    conductor.config = ConductorConfig()
    conductor.logger = logging.getLogger("test.verifier")
    conductor.problem_id = "test-problem"
    conductor.problem = None
    conductor._submission_lock = threading.Lock()
    conductor._submission_generation = 0
    conductor._aborted_submission_generations = set()
    future = Future() if pending_cleanup else None
    conductor._submit_future = future
    conductor._pending_submission_stages = {}
    conductor._verifier_runtime = previous
    process = object() if pending_cleanup else None
    previous._active_process = process

    def instantiate(_problem_id):
        fresh = conductor._get_verifier_runtime()
        assert fresh is not previous
        assert not fresh._cancelled.is_set()
        raise ValueError("stop before provisioning")

    conductor.problems = SimpleNamespace(get_problem_instance=instantiate)

    async def start():
        if future is None:
            return await conductor.start_problem()
        task = asyncio.create_task(conductor.start_problem())
        await asyncio.sleep(0)
        assert not task.done()
        assert conductor._verifier_runtime is previous
        assert previous._active_process is process
        # The submission/cleanup future completes only after runtime cleanup
        # has released its worker. The new attempt must wait for that point.
        previous._active_process = None
        future.set_result(None)
        return await task

    with pytest.raises(ValueError, match="stop before provisioning"):
        asyncio.run(start())
    assert previous._cancelled.is_set()
    assert conductor._submission_generation == 1


def test_every_conductor_configuration_requires_independent_verification():
    assert ConductorConfig().verifier_isolation is True
    with pytest.raises(ValueError, match="required for every run"):
        ConductorConfig(verifier_isolation=False)


@pytest.mark.parametrize("success", [True, False])
def test_diagnosis_forwards_the_live_oracle_and_submission_to_the_verifier(success):
    conductor = Conductor.__new__(Conductor)
    conductor.config = ConductorConfig()
    conductor.logger = logging.getLogger("test.verifier")
    conductor.execution_start_time = 0
    conductor.problem = SimpleNamespace(diagnosis_oracle=Mock())
    conductor._verifier_runtime = Mock()
    conductor._verifier_runtime.evaluate.return_value = {"success": success, "accuracy": 100.0 if success else 0.0}
    solution = "checkout uses the incorrect upstream port"
    result = conductor._evaluate_diagnosis(solution)
    assert result == {"success": success, "accuracy": 100.0 if success else 0.0, "submission": solution}
    conductor._verifier_runtime.evaluate.assert_called_once_with(conductor.problem.diagnosis_oracle, solution)
    conductor.problem.diagnosis_oracle.evaluate.assert_not_called()


def test_diagnosis_container_error_fails_without_running_the_host_oracle():
    conductor = Conductor.__new__(Conductor)
    conductor.config = ConductorConfig()
    conductor.logger = logging.getLogger("test.verifier")
    conductor.execution_start_time = 0
    conductor.problem = SimpleNamespace(diagnosis_oracle=Mock())
    conductor._verifier_runtime = Mock()
    conductor._verifier_runtime.evaluate.side_effect = TimeoutError("deadline exceeded")
    result = conductor._evaluate_diagnosis("submitted diagnosis")
    assert result["success"] is False
    assert result["reason"] == "verifier_execution_failed"
    assert result["failure_class"] == "harness_error"
    assert result["submission"] == "submitted diagnosis"
    conductor._verifier_runtime.evaluate.assert_called_once_with(
        conductor.problem.diagnosis_oracle, "submitted diagnosis"
    )
    conductor.problem.diagnosis_oracle.evaluate.assert_not_called()


def test_conductor_uses_the_live_oracle_and_never_falls_back_after_container_error():
    conductor = Conductor.__new__(Conductor)
    conductor.config = ConductorConfig(verifier_isolation=True)
    conductor.logger = logging.getLogger("test.verifier")
    conductor.execution_start_time = 0
    conductor.problem = SimpleNamespace(mitigation_oracle=Mock())
    conductor._verifier_runtime = Mock()
    conductor._verifier_runtime.evaluate.side_effect = TimeoutError("deadline exceeded")
    result = conductor._evaluate_mitigation("")
    assert result["success"] is False
    assert result["failure_class"] == "harness_error"
    conductor._verifier_runtime.evaluate.assert_called_once_with(conductor.problem.mitigation_oracle)
    conductor.problem.mitigation_oracle.evaluate.assert_not_called()


@pytest.mark.parametrize("stages", [("diagnosis",), ("mitigation",), ("diagnosis", "mitigation")])
def test_preparation_precedes_baseline_and_injection(stages):
    conductor = Conductor.__new__(Conductor)
    conductor.config = ConductorConfig(verifier_isolation=True)
    conductor.logger = logging.getLogger("test.verifier")
    conductor.stage_sequence = [{"name": name} for name in stages]
    calls = []
    oracle = SimpleNamespace(capture_baseline=lambda: calls.append("baseline"))
    conductor.problem = SimpleNamespace(mitigation_oracle=oracle, inject_fault=lambda: calls.append("inject"))
    conductor._verifier_runtime = SimpleNamespace(prepare=lambda: calls.append("prepare"))
    conductor._inject_fault()
    assert calls == ["prepare", "baseline", "inject"]


def test_unavailable_verifier_stops_a_diagnosis_only_run_before_fault_injection():
    conductor = Conductor.__new__(Conductor)
    conductor.config = ConductorConfig()
    conductor.logger = logging.getLogger("test.verifier")
    conductor.stage_sequence = [{"name": "diagnosis"}]
    conductor.problem = SimpleNamespace(mitigation_oracle=Mock(), inject_fault=Mock())
    conductor._verifier_runtime = Mock()
    conductor._verifier_runtime.prepare.side_effect = VerifierError("container runtime unavailable")
    with pytest.raises(VerifierError, match="container runtime unavailable"):
        conductor._inject_fault()
    conductor.problem.mitigation_oracle.capture_baseline.assert_not_called()
    conductor.problem.inject_fault.assert_not_called()


def _protocol_runtime(monkeypatch, body, timeout=5):
    runtime = VerifierRuntime(timeout_seconds=timeout)
    runtime.image, runtime.network, runtime.kubeconfig = "sha256:" + "a" * 64, "bridge", {}
    script = "import json,sys,time; request=json.loads(sys.stdin.readline()); " + body
    monkeypatch.setattr(runtime, "docker_command", lambda name: [sys.executable, "-u", "-c", script])
    # The protocol test uses a real child process, with no Docker dependency.
    monkeypatch.setattr(
        "sregym.service.verifier_runtime.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    return runtime


@pytest.mark.parametrize("success", [True, False])
def test_pipe_preserves_real_pass_and_fail_verdicts(monkeypatch, success):
    body = f"print(json.dumps({{'run_id':request['run_id'],'type':'verdict','result':{{'success':{success!r},'detail':{{'baseline':2}}}}}}))"
    result = _protocol_runtime(monkeypatch, body).evaluate_snapshot(b"trusted-snapshot")
    assert result == {"success": success, "detail": {"baseline": 2}}


def test_unexpected_stderr_failure_cannot_accept_a_success_verdict(monkeypatch):
    runtime = _protocol_runtime(
        monkeypatch,
        "print(json.dumps({'run_id':request['run_id'],'type':'verdict','result':{'success':True}}))",
    )
    launch = verifier_runtime.subprocess.Popen

    def broken_stderr(*args, **kwargs):
        process = launch(*args, **kwargs)
        process.stderr.close()
        process.stderr = io.BytesIO()
        process.stderr.close()
        return process

    monkeypatch.setattr(verifier_runtime.subprocess, "Popen", broken_stderr)
    with pytest.raises(VerifierError):
        runtime.evaluate_snapshot(b"trusted-snapshot")
    assert runtime._active_process is None


@pytest.mark.parametrize(
    "body",
    [
        "print(json.dumps({'run_id':'stale','type':'verdict','result':{'success':True}}))",
        "print(json.dumps({'run_id':request['run_id'],'type':'verdict','result':{'success':'true'}}))",
        "print(json.dumps({'run_id':request['run_id'],'type':'verdict','result':{'success':True}})); sys.exit(7)",
        "print(json.dumps({'run_id':request['run_id'],'type':'verdict','result':{'success':True}})); print('forged trailing verdict')",
        "sys.exit(0)",
    ],
)
def test_forged_stale_malformed_and_crashed_workers_never_pass(monkeypatch, body):
    with pytest.raises(VerifierError):
        _protocol_runtime(monkeypatch, body).evaluate_snapshot(b"trusted-snapshot")


def test_deadline_kills_the_worker_and_releases_the_active_invocation(monkeypatch):
    runtime = _protocol_runtime(monkeypatch, "time.sleep(10)", timeout=0.1)
    with pytest.raises(TimeoutError):
        runtime.evaluate_snapshot(b"trusted-snapshot")
    assert runtime._active_process is None
    assert runtime._active_name is None


def test_dead_docker_client_still_removes_its_daemon_owned_container(monkeypatch):
    runtime = VerifierRuntime()
    runtime.image, runtime.network, runtime.kubeconfig = "sha256:" + "a" * 64, "bridge", {}
    process = Mock(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
    process.poll.return_value = 137
    launch, remove = Mock(return_value=process), Mock()
    monkeypatch.setattr(verifier_runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(verifier_runtime.subprocess, "run", remove)
    with pytest.raises(VerifierError, match="without a verdict"):
        runtime.evaluate_snapshot(b"trusted-snapshot")
    command = launch.call_args.args[0]
    name = command[command.index("--name") + 1]
    remove.assert_called_once_with(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)
    assert runtime._active_name is None
    assert runtime._active_process is None


def test_live_workload_io_cannot_outlast_the_grading_deadline(monkeypatch):
    body = (
        "print(json.dumps({'run_id':request['run_id'],'type':'resource','index':0,'op':'metrics','args':[]})); "
        "sys.stdin.readline(); time.sleep(10)"
    )
    runtime = _protocol_runtime(monkeypatch, body, timeout=0.1)
    release = threading.Event()
    workload = SimpleNamespace(metrics=SimpleNamespace(snapshot=lambda: release.wait(10)))
    try:
        with pytest.raises(TimeoutError):
            runtime.evaluate_snapshot(b"trusted-snapshot", [workload])
        assert runtime._active_process is None
    finally:
        release.set()


def test_cancel_kills_the_docker_client_even_if_container_removal_fails(monkeypatch):
    runtime = VerifierRuntime()
    runtime._active_name = "sregym-verifier-test"
    runtime._active_process = Mock()
    runtime._active_process.poll.return_value = None

    def fail(*args, **kwargs):
        raise FileNotFoundError("docker unavailable")

    monkeypatch.setattr("sregym.service.verifier_runtime.subprocess.run", fail)
    with pytest.raises(FileNotFoundError):
        runtime.cancel()
    runtime._active_process.kill.assert_called_once()


def test_unreadable_node_probe_cannot_look_like_a_removed_eviction_threshold(monkeypatch):
    monkeypatch.setenv("SREGYM_VERIFIER_CONTAINER", "1")
    problem = TestProblem()
    problem.kubectl = Mock()
    oracle = KubeletEvictionThresholdMisconfigMitigationOracle(problem)
    problem.kubectl.run_node_script_pod.return_value = ""
    with pytest.raises(RuntimeError, match="no kubelet configuration"):
        oracle._read_kubelet_config(None, "worker")
    problem.kubectl.run_node_script_pod.return_value = (
        "kind: KubeletConfiguration\nevictionHard:\n  nodefs.available: 10%"
    )
    assert oracle._read_kubelet_config(None, "worker") == "  nodefs.available: 10%"
    problem.kubectl.run_node_script_pod.return_value = "kind: KubeletConfiguration"
    assert oracle._read_kubelet_config(None, "worker") == ""
    assert problem.kubectl.run_node_script_pod.call_args.kwargs["namespace"] == VERIFIER_PROBE_NAMESPACE


@pytest.mark.parametrize("suffix", ["", "/probe", "/probe/exec", "/probe/log"])
def test_node_probe_api_paths_are_hidden_from_the_agent(suffix):
    assert _is_hidden_namespace_request(
        f"/api/v1/namespaces/{VERIFIER_PROBE_NAMESPACE}/pods{suffix}", HIDDEN_NAMESPACES
    )


@pytest.mark.parametrize("value", [-1, True, 1.0, "1024", 8 * 1024**3 + 1])
def test_scratch_budget_rejects_invalid_values_before_preparation(monkeypatch, value):
    runtime = VerifierRuntime()
    prepare = Mock()
    monkeypatch.setattr(runtime, "prepare", prepare)
    with pytest.raises(VerifierError, match="scratch budget"):
        runtime.evaluate_snapshot(b"trusted", scratch_bytes=value)
    prepare.assert_not_called()


def test_scratch_zero_preserves_the_default_command_and_creates_no_volume(monkeypatch):
    runtime = _protocol_runtime(
        monkeypatch, "print(json.dumps({'run_id':request['run_id'],'type':'verdict','result':{'success':True}}))"
    )
    capacity = Mock(side_effect=AssertionError("default must not inspect scratch"))
    monkeypatch.setattr(runtime, "_scratch_host_capacity", capacity)
    assert runtime.evaluate_snapshot(b"trusted", scratch_bytes=0) == {"success": True}
    capacity.assert_not_called()


def test_scratch_insufficient_capacity_never_creates_a_volume_or_runs_an_oracle(monkeypatch):
    runtime, commands, _state = _scratch_protocol_runtime(monkeypatch)
    monkeypatch.setattr(
        runtime, "_scratch_host_capacity", Mock(side_effect=VerifierError("insufficient local capacity"))
    )
    with pytest.raises(VerifierError, match="capacity"):
        runtime.evaluate_snapshot(b"trusted", scratch_bytes=1024**3)
    assert commands == [] and runtime._active_run_id is None


def test_scratch_initializer_refuses_a_mutable_image_before_inspection_or_launch(monkeypatch):
    runtime = VerifierRuntime(docker_host="unix:///var/run/docker.sock")
    runtime.image = "some-registry/verifier:latest"
    launch, inspect = Mock(), Mock()
    monkeypatch.setattr(verifier_runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(runtime, "_inspect_scratch", inspect)
    with pytest.raises(VerifierError, match="immutable"):
        runtime._initialize_scratch(verifier_runtime._ScratchVolume("owned", "run", "owner"), 1024, lambda: 10)
    launch.assert_not_called()
    inspect.assert_not_called()


def test_owner_oracle_declares_the_scratch_budget_without_host_grading(monkeypatch):
    runtime = VerifierRuntime()
    runtime.kubeconfig, runtime.network = {}, "bridge"
    oracle = SimpleNamespace(verification_scratch_bytes=1024**3, problem=SimpleNamespace(), evaluate=Mock())
    monkeypatch.setattr(verifier_runtime, "verifier_connection", lambda *a, **kw: ({}, "bridge"))
    monkeypatch.setattr(verifier_runtime, "snapshot_oracle", lambda *a: (b"trusted", []))
    snapshot = Mock(return_value={"success": True})
    monkeypatch.setattr(runtime, "evaluate_snapshot", snapshot)
    assert runtime.evaluate(oracle) == {"success": True}
    assert snapshot.call_args.kwargs["scratch_bytes"] == 1024**3
    oracle.evaluate.assert_not_called()


@pytest.mark.parametrize("value", [True, 0, 255, 2049, 512.0, "512"])
def test_invalid_process_budget_refuses_before_any_verifier_preparation(monkeypatch, value):
    runtime = VerifierRuntime()
    prepare = Mock()
    monkeypatch.setattr(runtime, "prepare", prepare)
    with pytest.raises(VerifierError, match="process budget"):
        runtime.evaluate_snapshot(b"trusted", process_limit=value)
    prepare.assert_not_called()


def test_private_process_budget_preserves_container_isolation_and_cpu_memory_limits():
    runtime = VerifierRuntime()
    runtime.image, runtime.network = "sha256:" + "a" * 64, "bridge"
    command = runtime.docker_command("owned", process_limit=784)
    assert "--pids-limit=784" in command and "--env=GOMAXPROCS=2" in command
    assert {"--cpus=2", "--memory=2g", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"} <= set(
        command
    )
    assert not any("privileged" in argument or "docker.sock" in argument for argument in command)


@pytest.mark.parametrize("resource", ["cpu_limit", "memory_gib_limit"])
@pytest.mark.parametrize("value", [True, 0, 9, -1, 4.0, "4"])
def test_invalid_worker_capacity_refuses_before_any_preparation(monkeypatch, resource, value):
    runtime = VerifierRuntime()
    prepare = Mock()
    monkeypatch.setattr(runtime, "prepare", prepare)
    with pytest.raises(VerifierError, match="budget"):
        runtime.evaluate_snapshot(b"trusted", **{resource: value})
    prepare.assert_not_called()


@pytest.mark.parametrize("capacity", [2, 4, 8])
def test_larger_trusted_worker_capacity_retains_all_isolation_controls(capacity):
    runtime = VerifierRuntime()
    runtime.image, runtime.network = "sha256:" + "a" * 64, "bridge"
    command = runtime.docker_command("owned", process_limit=784, cpu_limit=capacity, memory_gib_limit=capacity)
    assert {
        f"--cpus={capacity}",
        f"--memory={capacity}g",
        "--user=10001:10001",
        "--pids-limit=784",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--env=GOMAXPROCS=2",
    } <= set(command)
    assert not any("privileged" in option or "docker.sock" in option for option in command)


def test_frozen_oracle_capacity_reaches_worker_launch_without_host_evaluation(monkeypatch):
    runtime = VerifierRuntime()
    runtime.kubeconfig, runtime.network = {}, "bridge"
    oracle = SimpleNamespace(
        verification_cpu_limit=8, verification_memory_gib_limit=8, problem=SimpleNamespace(), evaluate=Mock()
    )
    monkeypatch.setattr(verifier_runtime, "verifier_connection", lambda *a, **kw: ({}, "bridge"))
    monkeypatch.setattr(verifier_runtime, "snapshot_oracle", lambda *a: (b"trusted", []))
    snapshot = Mock(return_value={"success": True})
    monkeypatch.setattr(runtime, "evaluate_snapshot", snapshot)
    assert runtime.evaluate(oracle) == {"success": True}
    assert snapshot.call_args.kwargs["cpu_limit"] == snapshot.call_args.kwargs["memory_gib_limit"] == 8
    oracle.evaluate.assert_not_called()


def test_preparation_refreshes_process_budget_before_serializing_verification(monkeypatch):
    runtime = VerifierRuntime()
    runtime.kubeconfig, runtime.network = {}, "bridge"
    oracle = SimpleNamespace(verification_process_limit=256, evaluate=Mock())
    oracle.problem = SimpleNamespace(prepare_verification=lambda: setattr(oracle, "verification_process_limit", 784))
    monkeypatch.setattr(verifier_runtime, "verifier_connection", lambda *a, **kw: ({}, "bridge"))
    monkeypatch.setattr(verifier_runtime, "snapshot_oracle", lambda *a: (b"trusted", []))
    snapshot = Mock(return_value={"success": True})
    monkeypatch.setattr(runtime, "evaluate_snapshot", snapshot)
    assert runtime.evaluate(oracle) == {"success": True}
    assert snapshot.call_args.kwargs["process_limit"] == 784
    oracle.evaluate.assert_not_called()


def test_scratch_initializer_and_grader_mount_only_a_private_named_volume(monkeypatch):
    runtime, commands, _state = _scratch_protocol_runtime(monkeypatch)
    assert runtime.evaluate_snapshot(b"trusted", scratch_bytes=1024**3) == {"success": True}
    initializer = next(command for command in commands if "--entrypoint=python" in command)
    grader = next(command for command in commands if "--env=TMPDIR=/scratch" in command)
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--cap-add=CHOWN",
        "--user=0:0",
        "--memory=128m",
        "--memory-swap=128m",
        "--pids-limit=32",
    ):
        assert flag in initializer
    assert sum(flag.startswith("--cap-add") for flag in initializer) == 1
    assert all(
        "type=volume" in flag and "volume-nocopy" in flag
        for command in (initializer, grader)
        for flag in command
        if flag.startswith("--mount=")
    )
    assert not any(
        "docker.sock" in flag or "type=bind" in flag or flag == "--privileged"
        for command in (initializer, grader)
        for flag in command
        if not flag.startswith("unix://")
    )
    assert "--user=10001:10001" in grader and "--cap-drop=ALL" in grader
    assert "--env=SREGYM_VERIFIER_SCRATCH_BYTES=1073741824" in grader
    program = initializer[-1]
    assert program.index("os.chmod") < program.index("os.chown")
    assert "not any(p.iterdir())" in program and "10001,10001" in program
    assert any(command[-3:-1] == ["volume", "rm"] for command in commands)
    assert runtime._active_run_id is None


def _scratch_protocol_runtime(
    monkeypatch,
    *,
    init_failure=False,
    cleanup_failure=False,
    replace_owner=False,
    cancel_init=False,
    cancel_create=False,
):
    runtime = VerifierRuntime(docker_host="unix:///var/run/docker.sock", timeout_seconds=5)
    runtime.image, runtime.network, runtime.kubeconfig = "sha256:" + "a" * 64, "bridge", {}
    monkeypatch.setattr(runtime, "_scratch_host_capacity", lambda *a: None)
    commands, state = [], {"volume": None}
    real_launch = verifier_runtime.subprocess.Popen

    def docker(command, **kwargs):
        commands.append(command)
        tail = command[3:]
        if tail[:2] == ["volume", "create"]:
            labels = dict(value.split("=", 1) for i, value in enumerate(tail) if i and tail[i - 1] == "--label")
            state["volume"] = {
                "Name": tail[-1],
                "Driver": "local",
                "Scope": "local",
                "Options": None,
                "Labels": labels,
                "CreatedAt": "2026-10-09T01:02:03Z",
            }
            if cancel_create:
                runtime.cancel()
        elif tail[:2] == ["volume", "inspect"]:
            if state["volume"] is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="Error: no such volume")
            return SimpleNamespace(returncode=0, stdout=json.dumps([state["volume"]]), stderr="")
        elif tail[:2] == ["volume", "rm"]:
            if cleanup_failure:
                return SimpleNamespace(returncode=1, stdout="", stderr="volume remains in use")
            state["volume"] = None
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def launch(command, **kwargs):
        commands.append(command)
        if "--entrypoint=python" in command:
            process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
            process.returncode = 1 if init_failure else 0
            process.poll.side_effect = lambda: process.returncode
            process.kill.side_effect = lambda: setattr(process, "returncode", 137)

            def communicate(**kwargs):
                if cancel_init:
                    process.returncode = None
                    runtime.cancel()
                return b"", b""

            process.communicate.side_effect = communicate
            state["initializer"] = process
            return process
        if replace_owner:
            state["volume"]["CreatedAt"] = "2026-10-09T02:03:04Z"
        script = "import json,sys; request=json.loads(sys.stdin.readline()); print(json.dumps({'run_id':request['run_id'],'type':'verdict','result':{'success':True}}))"
        return real_launch([sys.executable, "-u", "-c", script], **kwargs)

    monkeypatch.setattr(verifier_runtime.subprocess, "run", docker)
    monkeypatch.setattr(verifier_runtime.subprocess, "Popen", launch)
    return runtime, commands, state


@pytest.mark.parametrize("failure", ["init", "create-cancel", "init-cancel"])
def test_scratch_partial_setup_and_cancellation_remove_only_the_owned_volume(monkeypatch, failure):
    runtime, commands, state = _scratch_protocol_runtime(
        monkeypatch,
        init_failure=failure == "init",
        cancel_create=failure == "create-cancel",
        cancel_init=failure == "init-cancel",
    )
    with pytest.raises(VerifierError):
        runtime.evaluate_snapshot(b"trusted", scratch_bytes=1024**3)
    assert state["volume"] is None
    assert any(command[3:5] == ["volume", "rm"] for command in commands)
    assert not any("--env=TMPDIR=/scratch" in command for command in commands)
    assert runtime._active_run_id is None and runtime._active_process is None
    if failure == "init-cancel":
        state["initializer"].kill.assert_called_once()


def test_scratch_owner_replacement_cannot_be_deleted_or_return_a_passing_verdict(monkeypatch):
    runtime, commands, state = _scratch_protocol_runtime(monkeypatch, replace_owner=True)
    with pytest.raises(VerifierError, match="ownership changed"):
        runtime.evaluate_snapshot(b"trusted", scratch_bytes=1024**3)
    assert state["volume"] is not None
    assert not any(command[3:5] == ["volume", "rm"] for command in commands)
    assert runtime._active_run_id is None


def test_scratch_cleanup_failure_blocks_a_completed_success_verdict(monkeypatch):
    runtime, _commands, state = _scratch_protocol_runtime(monkeypatch, cleanup_failure=True)
    with pytest.raises(VerifierError, match="could not be removed"):
        runtime.evaluate_snapshot(b"trusted", scratch_bytes=1024**3)
    assert state["volume"] is not None and runtime._active_run_id is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("Name", "other-volume"),
        ("Driver", "nfs"),
        ("Options", {"type": "none", "o": "bind", "device": "/"}),
        ("Labels", {}),
        ("CreatedAt", ""),
    ],
)
def test_scratch_inspection_rejects_foreign_storage_before_removal(monkeypatch, field, value):
    runtime = VerifierRuntime(docker_host="unix:///var/run/docker.sock")
    volume = verifier_runtime._ScratchVolume("sregym-verifier-scratch-test", "run", "owner")
    info = {
        "Name": volume.name,
        "Driver": "local",
        "Scope": "local",
        "Options": None,
        "Labels": volume.labels,
        "CreatedAt": "original",
    }
    info[field] = value
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps([info]), stderr=""))
    monkeypatch.setattr(verifier_runtime.subprocess, "run", run)
    with pytest.raises(VerifierError, match="ownership changed"):
        runtime._remove_scratch(volume)
    assert run.call_count == 1


@pytest.mark.parametrize("failure", [None, "nonroot", "rootless", "capacity", "workload"])
def test_scratch_capacity_requires_a_separate_local_rootful_trusted_engine(monkeypatch, failure):
    runtime = VerifierRuntime(docker_host="unix:///var/run/docker.sock")

    class LocalPath:
        def __init__(self, value):
            self.value = value

        def is_absolute(self):
            return True

        def is_symlink(self):
            return False

        def is_dir(self):
            return True

        def stat(self):
            return SimpleNamespace(st_uid=1001 if failure == "nonroot" else 0, st_mode=stat.S_IFSOCK)

    monkeypatch.setattr(verifier_runtime, "Path", LocalPath)
    monkeypatch.setattr(
        verifier_runtime,
        "os",
        SimpleNamespace(
            name="posix",
            environ={
                "DOCKER_HOST": runtime.docker_host if failure == "workload" else "unix:///run/user/1001/docker.sock"
            },
        ),
    )
    info = {
        "DockerRootDir": "/var/lib/docker",
        "OSType": "linux",
        "SecurityOptions": ["name=rootless"] if failure == "rootless" else [],
    }
    monkeypatch.setattr(verifier_runtime.subprocess, "run", Mock(return_value=SimpleNamespace(stdout=json.dumps(info))))
    monkeypatch.setattr(
        verifier_runtime.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1024 if failure == "capacity" else 4 * 1024**3),
    )
    if failure is None:
        runtime._scratch_host_capacity(1024**3, lambda: 10)
    else:
        with pytest.raises(VerifierError):
            runtime._scratch_host_capacity(1024**3, lambda: 10)


def test_scratch_preparation_reserves_the_invocation_without_second_call_clearing_it(monkeypatch):
    runtime, _commands, _state = _scratch_protocol_runtime(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def capacity(*args):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(runtime, "_scratch_host_capacity", capacity)
    results = []

    def owner():
        try:
            results.append(runtime.evaluate_snapshot(b"trusted", scratch_bytes=1024**3))
        except Exception as exc:
            results.append(exc)

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(VerifierError, match="already active"):
            runtime.evaluate_snapshot(b"trusted")
        assert runtime._active_run_id is not None
    finally:
        release.set()
        thread.join(3)
    assert results == [{"success": True}] and runtime._active_run_id is None
