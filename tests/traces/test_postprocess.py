"""Tests for the host-side postprocess walker."""

import json
import shutil
from pathlib import Path

from sregym.traces import postprocess
from sregym.traces.atif import Trajectory

FIXTURE_RUN = Path(__file__).parent / "fixtures" / "claudecode_run"


def _make_results_tree(tmp_path: Path, *, run_names=("run_1",)) -> Path:
    results = tmp_path / "results"
    for run_name in run_names:
        run_dir = results / "0629_1125" / "claudecode" / "service_port_conflict_hotel_reservation" / run_name
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(FIXTURE_RUN, run_dir)
    return results


def test_postprocess_tree_writes_trajectory_json(tmp_path):
    results = _make_results_tree(tmp_path, run_names=("run_1", "run_2"))
    written = postprocess.postprocess(results)
    assert len(written) == 2
    for out in written:
        assert out.name == "trajectory.json"
        payload = json.loads(out.read_text())
        # Valid ATIF.
        Trajectory.model_validate(payload)
        assert payload["extra"]["sregym"]["problem_id"] == ("service_port_conflict_hotel_reservation")


def test_postprocess_is_idempotent(tmp_path):
    results = _make_results_tree(tmp_path)
    first = postprocess.postprocess(results)
    out = first[0]
    bytes_first = out.read_bytes()
    second = postprocess.postprocess(results)
    assert second == first
    assert out.read_bytes() == bytes_first


def test_postprocess_skips_unconvertible_run(tmp_path):
    # A run dir with no sessions/ must be skipped without raising.
    results = tmp_path / "results"
    empty_run = results / "0629_1125" / "claudecode" / "kubelet_crash" / "run_1"
    empty_run.mkdir(parents=True)
    (empty_run / "driver.log").write_text("nothing useful")
    written = postprocess.postprocess(results)
    assert written == []
    assert not (empty_run / "trajectory.json").exists()


def test_write_trajectory_single_published_dir(tmp_path):
    # Mirrors the call main.py makes right after finalize_and_publish:
    # write_trajectory(published_run_dir) writes trajectory.json, non-fatally.
    results = _make_results_tree(tmp_path)
    run_dir = results / "0629_1125" / "claudecode" / "service_port_conflict_hotel_reservation" / "run_1"
    out = postprocess.write_trajectory(run_dir)
    assert out == run_dir / "trajectory.json"
    assert out.exists()
    Trajectory.model_validate(json.loads(out.read_text()))


def test_write_trajectory_survives_value_error(tmp_path):
    # A path whose leaf is not run_<n> makes parse_run_path raise ValueError,
    # which write_trajectory catches and returns None.
    run_dir = tmp_path / "results" / "b" / "claudecode" / "p" / "not_a_run"
    run_dir.mkdir(parents=True)
    assert postprocess.write_trajectory(run_dir) is None


def test_write_trajectory_survives_unexpected_exception(tmp_path, monkeypatch):
    # A non-ValueError exception (e.g. TypeError deep in the adapter) must NOT
    # propagate — this is the whole reason write_trajectory is called from the
    # run pipeline. Inject a real exception into convert.convert_run.
    from sregym.traces import convert

    run_dir = tmp_path / "results" / "b" / "claudecode" / "p_hotel_reservation" / "run_1"
    run_dir.mkdir(parents=True)

    def _boom(_run_dir):
        raise TypeError("synthetic corruption")

    monkeypatch.setattr(convert, "convert_run", _boom)
    assert postprocess.write_trajectory(run_dir) is None
