import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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
    ("stages", "external", "expected_calls"),
    [(None, False, 1), (["diagnosis"], False, 1), (["mitigation"], False, 0), (None, True, 0)],
)
def test_judge_preflight_only_when_diagnosis_can_run(monkeypatch, stages, external, expected_calls):
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
    )
    with pytest.raises(ReachedConductor):
        benchmark_main.main(args)
    assert len(calls) == expected_calls
