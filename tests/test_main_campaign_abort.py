import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.mark.parametrize("platform_failure, expected_attempts", [(True, 1), (False, 3)])
def test_deployment_retries_only_transient_failures(monkeypatch, tmp_path, platform_failure, expected_attempts):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    error_type = benchmark_main.ContainerPlatformError if platform_failure else RuntimeError
    conductor = SimpleNamespace(
        problems=Mock(get_problem_ids=Mock(return_value=["problem"])),
        results={},
        start_problem=AsyncMock(side_effect=error_type("image could not start")),
        finish_problem_in_background=Mock(),
        wait_for_submission_work=AsyncMock(),
    )

    results = benchmark_main.driver_loop(conductor, use_external_harness=True)

    assert conductor.start_problem.await_count == expected_attempts
    assert conductor.finish_problem_in_background.call_count == expected_attempts
    assert conductor.wait_for_submission_work.await_count == expected_attempts
    assert results == [{None: [{"problem_id": "problem", "attempt": 1, "deploy_failed": True}]}]
