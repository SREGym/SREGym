import importlib.util
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest


def _load_main_module():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_benchmark_main_for_test", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_driver_wrapper_preserves_partial_results_and_failure(monkeypatch):
    benchmark_main = _load_main_module()
    partial_results = [
        {
            "codex": [
                {
                    "problem_id": "problem",
                    "attempt": 1,
                    "run_status": "incomplete",
                    "incomplete_reason": "cleanup_timeout_after_agent_exit",
                }
            ]
        }
    ]

    def abort_driver(*_args, **_kwargs):
        raise benchmark_main.BenchmarkCampaignAborted("cleanup timed out", partial_results)

    shutdown_called = []
    monkeypatch.setattr(benchmark_main, "driver_loop", abort_driver)
    monkeypatch.setattr(benchmark_main.LAUNCHER, "cleanup_all", lambda: None)
    monkeypatch.setattr(benchmark_main, "request_shutdown", lambda: shutdown_called.append(True))

    benchmark_main._run_driver_and_shutdown(object())

    assert benchmark_main._driver_results == partial_results
    assert isinstance(benchmark_main._driver_error, benchmark_main.BenchmarkCampaignAborted)
    assert shutdown_called == [True]


def test_interrupted_problem_uses_the_bounded_cleanup_budget():
    benchmark_main = _load_main_module()
    cleanup = Mock()
    conductor = SimpleNamespace(
        problem=SimpleNamespace(cleanup_timeout_seconds=42),
        finish_problem_in_background=Mock(return_value=cleanup),
        close_submissions=Mock(return_value=False),
    )

    benchmark_main.cleanup_interrupted_problem(conductor)

    cleanup.result.assert_called_once_with(timeout=benchmark_main.CLEANUP_DRAIN_TIMEOUT_SECONDS)


@pytest.mark.parametrize("evaluation_stuck", [False, True])
def test_interrupt_drains_evaluation_before_cleanup(evaluation_stuck):
    benchmark_main = _load_main_module()
    conductor = SimpleNamespace(
        problem=SimpleNamespace(),
        close_submissions=Mock(return_value=True),
        wait_for_submission_evaluations=AsyncMock(side_effect=TimeoutError if evaluation_stuck else None),
        finish_problem_in_background=Mock(),
        abandon_submission_work=Mock(),
    )

    benchmark_main.cleanup_interrupted_problem(conductor)

    conductor.wait_for_submission_evaluations.assert_awaited_once_with(
        timeout=benchmark_main.EVALUATION_DRAIN_TIMEOUT_SECONDS
    )
    if evaluation_stuck:
        conductor.finish_problem_in_background.assert_not_called()
        conductor.abandon_submission_work.assert_called_once()
    else:
        conductor.finish_problem_in_background.assert_called_once()


def test_stopped_driver_does_not_start_deployment(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    stop_event = threading.Event()
    stop_event.set()
    conductor = SimpleNamespace(start_problem=AsyncMock())

    with pytest.raises(benchmark_main.BenchmarkCampaignAborted, match="interrupted"):
        benchmark_main.driver_loop(conductor, use_external_harness=True, stop_event=stop_event)
    conductor.start_problem.assert_not_awaited()


def test_interrupt_stops_agent_before_driver_owned_teardown(monkeypatch):
    benchmark_main = _load_main_module()
    entered_driver = threading.Event()
    entered_cleanup = threading.Event()
    agent_running = [True]
    cleanup_active = [False]
    overlapping_cleanup = []
    cleanup_states = []
    finished = Future()
    finished.set_result(None)

    def finish():
        cleanup_states.append(agent_running[0])
        return finished

    conductor = SimpleNamespace(
        problem=SimpleNamespace(),
        finish_problem_in_background=finish,
        close_submissions=Mock(return_value=False),
        stop_k8s_proxy=Mock(),
    )

    def driver(*args, stop_event, **kwargs):
        entered_driver.set()
        assert stop_event.wait(2)
        assert entered_cleanup.wait(2)
        raise benchmark_main.BenchmarkCampaignAborted("interrupted", [])

    def stop_agents():
        if cleanup_active[0]:
            overlapping_cleanup.append(True)
        cleanup_active[0] = True
        entered_cleanup.set()
        time.sleep(0.02)
        agent_running[0] = False
        cleanup_active[0] = False

    def interrupt(_):
        assert entered_driver.wait(2)
        raise KeyboardInterrupt

    monkeypatch.setattr(benchmark_main, "Conductor", lambda **kwargs: conductor)
    monkeypatch.setattr(benchmark_main, "_configure_model_environment", lambda args: ("unused", "unused"))
    monkeypatch.setattr(benchmark_main, "set_profile", lambda profile: None)
    monkeypatch.setattr(benchmark_main, "driver_loop", driver)
    monkeypatch.setattr(benchmark_main, "run_api", interrupt)
    monkeypatch.setattr(benchmark_main, "request_shutdown", Mock())
    monkeypatch.setattr(benchmark_main.LAUNCHER, "cleanup_all", stop_agents)
    monkeypatch.setattr(benchmark_main.LAUNCHER, "enable_container_isolation", Mock())
    monkeypatch.setattr(benchmark_main.LAUNCHER, "_container_runner", None)
    monkeypatch.setattr(benchmark_main, "run_preflight_check", Mock())
    args = SimpleNamespace(
        agent=None,
        internet_access="open",
        container_hardening="on",
        profile="full",
        noise=False,
        use_external_harness=False,
        force_build=False,
        stages=["mitigation"],
        baseline=None,
        propagation=None,
        suite=None,
        problem=None,
        n_attempts=1,
        agent_timeout=30,
        resume=None,
    )

    with pytest.raises(RuntimeError, match="Benchmark campaign did not complete"):
        benchmark_main._run_benchmark(args)
    assert cleanup_states == [False]
    assert not overlapping_cleanup


def test_interrupt_after_setup_does_not_start_another_attempt(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    stop_event = threading.Event()

    async def finish_setup():
        stop_event.set()
        return object()

    conductor = SimpleNamespace(
        problems=Mock(get_problem_ids=Mock(return_value=["problem"])),
        clear_cluster_egress_boundary=Mock(),
        start_problem=AsyncMock(side_effect=finish_setup),
        bind_phase_ledger=Mock(),
    )
    with pytest.raises(benchmark_main.BenchmarkCampaignAborted, match="interrupted"):
        benchmark_main.driver_loop(conductor, use_external_harness=True, n_attempts=2, stop_event=stop_event)
    conductor.start_problem.assert_awaited_once()


def test_result_csv_publication_with_results_on_another_filesystem(tmp_path, monkeypatch):
    benchmark_main = _load_main_module()
    shared_memory = Path("/dev/shm")
    if not shared_memory.is_dir() or shared_memory.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("requires a second writable filesystem")
    monkeypatch.chdir(tmp_path)
    with tempfile.TemporaryDirectory(dir=shared_memory) as directory:
        partial, final = benchmark_main._problem_result_paths(Path(directory), "autosubmit", "problem")
        partial.write_text("run_status,Mitigation.success\ncomplete,False\n")
        benchmark_main.os.replace(partial, final)
        assert final.read_text() == "run_status,Mitigation.success\ncomplete,False\n"
        assert not partial.exists()


@pytest.mark.parametrize(
    ("stages", "external"),
    [(None, False), (["diagnosis"], False), (["mitigation"], False), (None, True)],
)
def test_judge_preflight_is_deferred_until_effective_stages_are_known(monkeypatch, stages, external):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main.os, "environ", benchmark_main.os.environ.copy())
    calls = []
    monkeypatch.setattr(benchmark_main, "init_logger", lambda: None)
    monkeypatch.setattr(benchmark_main, "_configure_model_environment", lambda args: ("unused", "unused"))
    monkeypatch.setattr(benchmark_main, "set_profile", lambda profile: None)
    monkeypatch.setattr(benchmark_main, "run_judge_preflight_check", lambda: calls.append(True))

    class ReachedConductor(Exception):
        pass

    def stop_before_deployment(**kwargs):
        raise ReachedConductor

    monkeypatch.setattr(benchmark_main, "ConductorConfig", stop_before_deployment)
    args = SimpleNamespace(
        agent=None,
        internet_access="open",
        container_hardening="on",
        profile="full",
        noise=False,
        use_external_harness=external,
        stages=stages,
        baseline=None,
        propagation=None,
    )
    with pytest.raises(ReachedConductor):
        benchmark_main._run_benchmark(args)
    assert calls == []


def test_cleanup_deadline_covers_namespace_deletion_and_honors_override(monkeypatch):
    monkeypatch.delenv("SREGYM_CLEANUP_DRAIN_TIMEOUT_S", raising=False)
    benchmark_main = _load_main_module()
    assert benchmark_main.CLEANUP_DRAIN_TIMEOUT_SECONDS == 600

    monkeypatch.setenv("SREGYM_CLEANUP_DRAIN_TIMEOUT_S", "900")
    benchmark_main = _load_main_module()
    assert benchmark_main.CLEANUP_DRAIN_TIMEOUT_SECONDS == 900


@pytest.mark.parametrize("platform_failure, expected_attempts", [(True, 1), (False, 3)])
@pytest.mark.parametrize("cleanup_budget", [None, 300, 900])
def test_deployment_retries_only_transient_failures(
    monkeypatch, tmp_path, platform_failure, expected_attempts, cleanup_budget
):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "full")
    error_type = benchmark_main.ContainerPlatformError if platform_failure else RuntimeError
    conductor = SimpleNamespace(
        problems=Mock(get_problem_ids=Mock(return_value=["problem"])),
        results={},
        bind_phase_ledger=Mock(),
        clear_cluster_egress_boundary=Mock(),
        start_problem=AsyncMock(side_effect=error_type("image could not start")),
        finish_problem_in_background=Mock(),
        wait_for_submission_work=AsyncMock(),
    )
    if cleanup_budget is not None:
        conductor.problem = SimpleNamespace(cleanup_timeout_seconds=cleanup_budget)

    results = benchmark_main.driver_loop(conductor, use_external_harness=True)

    assert conductor.start_problem.await_count == expected_attempts
    assert conductor.finish_problem_in_background.call_count == expected_attempts
    assert conductor.wait_for_submission_work.await_count == expected_attempts
    expected_cleanup_budget = max(
        benchmark_main.CLEANUP_DRAIN_TIMEOUT_SECONDS,
        cleanup_budget or benchmark_main.CLEANUP_DRAIN_TIMEOUT_SECONDS,
    )
    assert all(
        call.kwargs["timeout"] == expected_cleanup_budget for call in conductor.wait_for_submission_work.await_args_list
    )
    conductor.bind_phase_ledger.assert_called_once()
    assert results == [
        {None: [{"problem_id": "problem", "attempt": 1, "deployment_profile": "full", "deploy_failed": True}]}
    ]


def test_timed_out_agent_gets_the_normal_exit_reap_grace(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "full")
    monkeypatch.setattr(benchmark_main, "list_agents", lambda path: {"agent": object()})
    monkeypatch.setattr(benchmark_main.trace_postprocess, "write_trajectory", lambda _run_dir: None)
    run = Mock(finalize_and_publish=Mock(return_value=tmp_path / "run"))
    monkeypatch.setattr(benchmark_main, "RunArtifacts", SimpleNamespace(create=Mock(return_value=run)))
    cleanup_agent = Mock()
    monkeypatch.setattr(benchmark_main.LAUNCHER, "cleanup_agent", cleanup_agent)
    monkeypatch.setattr(benchmark_main.LAUNCHER, "set_agent_kubeconfig", Mock())
    monkeypatch.setattr(benchmark_main.LAUNCHER, "internet_policy_result", Mock(return_value={}))
    monkeypatch.setattr(benchmark_main.LAUNCHER, "_procs", {})
    conductor = SimpleNamespace(
        problems=Mock(get_problem_ids=Mock(return_value=["problem"])),
        results={},
        phases=None,
        stage_sequence=[],
        submission_stage="running",
        register_agent=Mock(),
        start_k8s_proxy=Mock(),
        stop_k8s_proxy=Mock(),
        get_agent_kubeconfig_path=Mock(return_value="kubeconfig"),
        bind_phase_ledger=Mock(),
        start_problem=AsyncMock(),
        close_submissions=Mock(return_value=False),
        missing_submission_stages=Mock(return_value=False),
        finish_problem_in_background=Mock(),
        wait_for_submission_work=AsyncMock(),
        finalize_attempt_status=Mock(return_value="incomplete"),
    )

    # A negative budget puts the deadline in the past before the first poll.
    benchmark_main.driver_loop(conductor, agent_to_run="agent", agent_timeout=-1)

    assert conductor.results["timed_out"] is True
    assert cleanup_agent.call_args_list == [
        call("agent", timeout=benchmark_main.AGENT_REAP_GRACE_SECONDS),
        call("agent"),
    ]
