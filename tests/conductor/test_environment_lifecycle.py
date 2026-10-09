"""Owned workload/noise hooks must not cross attempts or pause at submission."""

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor import conductor as conductor_module
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.problems.base import Problem


@pytest.fixture
def conductor():
    c = Conductor.__new__(Conductor)
    c.logger = logging.getLogger("test.environment_lifecycle")
    c.config = ConductorConfig(enable_noise=True, deploy_loki=False)
    c.problem = None
    c.results = {}
    c._submission_lock = threading.RLock()
    c._submission_generation = 1
    c._aborted_submission_generations = set()
    c._submit_future = None
    c._pending_submission_stages = {}
    c._verifier_runtime = None
    c._baseline_captured = False
    c._accepting_submissions = True
    c._attempt_closed = False
    c._evaluating = False
    c.waiting_for_agent = True
    c.current_stage_index = 0
    c.submission_stage = "diagnosis"
    c.execution_start_time = time.time()
    c.stage_sequence = [
        {"name": "diagnosis", "evaluation": lambda _: {"success": True}},
        {"name": "mitigation", "evaluation": lambda _: {"success": True}},
    ]
    return c


def _problem(events):
    return SimpleNamespace(
        run_default_noise=False,
        run_default_workload=True,
        prepare_environment=lambda **kw: events.append(("prepare", kw["enable_noise"])),
        stop_environment=lambda: events.append("stop"),
        recover_fault=lambda: events.append("recover"),
        baseline_duration_s=0,
        propagation_duration_s=0,
        app=SimpleNamespace(
            namespace="zone-a",
            name="service",
            deploy=lambda: events.append("deploy"),
            start_workload=lambda: events.append("workload"),
            cleanup=lambda: events.append("cleanup"),
        ),
    )


def test_problem_defaults_preserve_existing_workload_and_noise():
    assert Problem.run_default_workload is True
    assert Problem.run_default_noise is True
    assert Problem.prepare_environment(object(), enable_noise=True) is None
    assert Problem.stop_environment(object()) is None


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("task_default", [False, True, None])
def test_default_noise_selection(conductor, enabled, task_default):
    conductor.config.enable_noise = enabled
    problem = SimpleNamespace() if task_default is None else SimpleNamespace(run_default_noise=task_default)
    assert conductor._uses_default_noise(problem) is (enabled and task_default is not False)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("default_workload", [False, True])
def test_prepare_runs_after_real_app_deployment_and_selected_workload(
    conductor, monkeypatch, enabled, default_workload
):
    events = []
    conductor.problem = _problem(events)
    conductor.problem.run_default_workload = default_workload
    conductor.config.enable_noise = enabled
    conductor._baseline_captured = True
    conductor.kubectl = Mock()
    conductor.prometheus = Mock()
    conductor.jaeger = Mock()
    conductor.otel_collector = Mock()
    conductor.mcp_server = Mock()
    conductor._metrics_server_configured = lambda: True
    conductor._openebs_ready = lambda _: True
    conductor._trim_openebs_ndm = lambda: None
    conductor._wait_for_infrastructure_ready = lambda *_: None
    monkeypatch.setattr(conductor_module, "is_svelte", lambda: True)

    conductor.deploy_app()

    assert events == ["deploy", *(["workload"] if default_workload else []), ("prepare", enabled)]


@pytest.mark.parametrize("failure_at", ["deploy", "prepare"])
def test_partial_setup_stops_owned_environment(conductor, failure_at):
    events = []
    conductor.problem = _problem(events)
    if failure_at == "deploy":
        conductor._deploy_app = Mock(side_effect=RuntimeError("setup failed"))
    else:
        conductor._deploy_app = Mock()
        conductor.problem.prepare_environment = Mock(side_effect=RuntimeError("setup failed"))

    with pytest.raises(RuntimeError, match="setup failed"):
        conductor.deploy_app()

    assert events == ["stop"]
    conductor.problem.app.cleanup()  # Normal caller-owned resource teardown remains possible.
    assert events == ["stop", "cleanup"]


@pytest.mark.parametrize("default_noise", [False, True])
def test_cleanup_stops_producers_before_recovery_and_resource_deletion(conductor, monkeypatch, default_noise):
    events = []
    conductor.problem = _problem(events)
    conductor.problem.run_default_noise = default_noise
    conductor._baseline_captured = True
    conductor.cluster_state = SimpleNamespace(reconcile_to_baseline=lambda: events.append("reconcile") or {})
    noise = Mock()
    noise.stop.side_effect = lambda: events.append("default-noise-stop")
    monkeypatch.setattr(conductor_module, "get_noise_manager", lambda: noise)

    conductor._cleanup_sync()

    assert events == ["stop", *(["default-noise-stop"] if default_noise else []), "recover", "cleanup", "reconcile"]
    assert conductor.submission_stage == "done"


def test_failed_controller_stop_prevents_resource_teardown(conductor):
    conductor.problem = _problem([])
    conductor.problem.stop_environment = Mock(side_effect=TimeoutError("controller still running"))
    conductor.problem.recover_fault = Mock()
    conductor.problem.app.cleanup = Mock()

    with pytest.raises(TimeoutError, match="controller still running"):
        conductor._cleanup_sync()

    conductor.problem.recover_fault.assert_not_called()
    conductor.problem.app.cleanup.assert_not_called()
    assert conductor.submission_stage != "done"


def test_stale_cleanup_does_not_stop_a_replacement_environment(conductor, monkeypatch):
    events = []
    conductor.problem = _problem(events)
    conductor._submission_generation = 2
    conductor._verifier_runtime = Mock()
    noise = Mock()
    monkeypatch.setattr(conductor_module, "get_noise_manager", lambda: noise)

    conductor._cleanup_sync(generation=1)

    assert events == []
    conductor._verifier_runtime.cancel.assert_not_called()
    noise.stop.assert_not_called()


def test_task_owned_noise_survives_submission_and_stage_transition(conductor, monkeypatch):
    events = []
    conductor.problem = _problem(events)
    default_noise = Mock(side_effect=AssertionError("default manager must not touch task-owned noise"))
    monkeypatch.setattr(conductor_module, "get_noise_manager", default_noise)

    conductor._submit_evaluate_and_advance("answer", conductor.stage_sequence[0], 1)
    conductor.fault_injected = True
    asyncio.run(conductor._advance_to_next_stage(start_index=1))

    assert events == []
    default_noise.assert_not_called()
    assert conductor.results["Diagnosis"]["success"] is True
    assert conductor.submission_stage == "mitigation"


def test_abandonment_stops_captured_problem_without_blocking_driver(conductor):
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    replacement_stop = Mock()

    def stop():
        entered.set()
        release.wait(2)
        stopped.set()

    conductor.problem = SimpleNamespace(stop_environment=stop)
    try:
        started = time.monotonic()
        conductor.abandon_submission_work()
        assert time.monotonic() - started < 0.5
        assert entered.wait(1)
        conductor.problem = SimpleNamespace(stop_environment=replacement_stop)
    finally:
        release.set()
    assert stopped.wait(1)
    replacement_stop.assert_not_called()
    assert conductor.submission_stage == "aborted"


def test_undeploy_stops_captured_environment_before_deleting_app(conductor):
    events = []
    conductor.problem = _problem(events)
    conductor.undeploy_app()
    assert events == ["stop", "cleanup"]


def _configure_start(conductor, monkeypatch, events):
    problem = _problem(events)
    conductor.problem_id = "regional"
    conductor.problems = SimpleNamespace(get_problem_instance=lambda _: problem)
    conductor.dependency_check = Mock()
    conductor.fix_kubernetes = Mock()
    conductor.get_problem_stages = Mock()
    conductor._build_stage_sequence = Mock()
    conductor.undeploy_app = Mock()
    conductor._deploy_app = Mock()
    monkeypatch.setattr(conductor_module, "DetectionOracle", lambda _: object())

    async def advance(*, start_index):
        conductor.submission_stage = "diagnosis"

    conductor._advance_to_next_stage = advance
    return problem


def test_task_owned_noise_receives_config_without_starting_default_manager(conductor, monkeypatch):
    events = []
    _configure_start(conductor, monkeypatch, events)
    default_noise = Mock(side_effect=AssertionError("unexpected built-in noise"))
    monkeypatch.setattr(conductor_module, "get_noise_manager", default_noise)

    asyncio.run(conductor.start_problem())

    assert events == [("prepare", True)]
    default_noise.assert_not_called()


def test_cancelled_baseline_wait_stops_owned_environment(conductor, monkeypatch):
    events = []
    _configure_start(conductor, monkeypatch, events)
    conductor.config.baseline_override_s = 100

    async def run():
        task = asyncio.create_task(conductor.start_problem())
        while ("prepare", True) not in events and not task.done():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert events == [("prepare", True), "stop"]


def test_fault_setup_failure_stops_owned_environment(conductor, monkeypatch):
    events = []
    _configure_start(conductor, monkeypatch, events)

    async def fail_advance(**_):
        raise RuntimeError("fault setup failed")

    conductor._advance_to_next_stage = fail_advance
    with pytest.raises(RuntimeError, match="fault setup failed"):
        asyncio.run(conductor.start_problem())
    assert events == [("prepare", True), "stop"]


def test_partial_setup_cleanup_uses_captured_problem(conductor):
    events = []
    conductor.problem = _problem(events)
    replacement = _problem([])
    replacement.stop_environment = Mock()

    def failed_deploy(_):
        conductor.problem = replacement
        raise RuntimeError("deployment failed")

    conductor._deploy_app = failed_deploy
    with pytest.raises(RuntimeError, match="deployment failed"):
        conductor.deploy_app()
    assert events == ["stop"]
    replacement.stop_environment.assert_not_called()
