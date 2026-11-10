import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest


def _load_main_module():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_benchmark_main_for_results_dir_test", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runs_started_in_the_same_minute_get_separate_directories(tmp_path, monkeypatch):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "get_current_datetime_formatted", lambda: "1106_1358")
    root = tmp_path / "results"

    names = [benchmark_main.claim_results_dir(root).name for _ in range(3)]

    assert names == ["1106_1358", "1106_1358_2", "1106_1358_3"]


def test_concurrent_claims_never_share_a_directory(tmp_path, monkeypatch):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "get_current_datetime_formatted", lambda: "1106_1358")
    root = tmp_path / "results"

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: benchmark_main.claim_results_dir(root), range(16)))

    assert len(set(claimed)) == 16
    assert all(d.is_dir() for d in claimed)


def test_an_existing_file_with_the_name_is_skipped(tmp_path, monkeypatch):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "get_current_datetime_formatted", lambda: "1106_1358")
    root = tmp_path / "results"
    root.mkdir()
    (root / "1106_1358").write_text("not a directory")

    assert benchmark_main.claim_results_dir(root).name == "1106_1358_2"


def test_driver_loop_claims_its_directory(monkeypatch):
    benchmark_main = _load_main_module()

    class Claimed(Exception):
        pass

    def claim():
        raise Claimed

    monkeypatch.setattr(benchmark_main, "claim_results_dir", claim)
    with pytest.raises(Claimed):
        benchmark_main.driver_loop(Mock(), ["test_fault"], "codex")
