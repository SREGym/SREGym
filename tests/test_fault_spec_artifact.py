"""Each published attempt carries the injected fault's ground truth as fault-spec.json."""

import importlib.util
import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sregym.conductor.conductor import Conductor
from sregym.run_artifacts import RunArtifacts


def _load_main_module():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_benchmark_main_for_fault_spec_test", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Problem:
    namespace = "hotel-reservation"
    root_cause = "[fault_spec] component=frontend; namespace=hotel-reservation || frontend has the wrong port"
    mitigation_oracle = None
    diagnosis_oracle = None

    def __init__(self):
        self.injected = False

    def inject_fault(self):
        self.injected = True


def _bare_conductor(problem) -> Conductor:
    """A Conductor without __init__, so no cluster services are built."""
    conductor = object.__new__(Conductor)
    conductor.problem = problem
    conductor.problem_id = "test_fault"
    conductor.logger = logging.getLogger("test.fault_spec")
    conductor.fault_injected = False
    conductor.fault_spec = None
    return conductor


def test_injection_captures_the_spec_in_memory():
    problem = _Problem()
    conductor = _bare_conductor(problem)

    conductor._inject_fault()

    assert problem.injected
    assert conductor.fault_injected
    assert isinstance(conductor.fault_spec["fault_injected_at"], float)
    assert conductor.fault_spec == {
        "problem_id": "test_fault",
        "namespace": "hotel-reservation",
        "root_cause": _Problem.root_cause,
        "fault_injected_at": conductor.fault_spec["fault_injected_at"],
    }


def test_the_next_attempt_does_not_inherit_the_spec():
    conductor = _bare_conductor(_Problem())
    conductor._inject_fault()
    conductor.tasklist = []
    conductor.stage_sequence = []
    conductor.current_stage_index = 0
    conductor.waiting_for_agent = False
    conductor._evaluating = False
    conductor._accepting_submissions = False

    conductor._build_stage_sequence()

    assert conductor.fault_injected is False
    assert conductor.fault_spec is None


def test_published_attempt_gets_fault_spec_json(tmp_path):
    benchmark_main = _load_main_module()
    conductor = SimpleNamespace(
        fault_spec={
            "problem_id": "test_fault",
            "namespace": "hotel-reservation",
            "root_cause": _Problem.root_cause,
            "fault_injected_at": 1762455600.5,
        }
    )

    path = benchmark_main._write_fault_spec(tmp_path, conductor, "test_fault")

    assert path == tmp_path / "fault-spec.json"
    assert json.loads(path.read_text()) == {
        "problem_id": "test_fault",
        "namespace": "hotel-reservation",
        "root_cause": _Problem.root_cause,
        "fault_injected_at": "2025-11-06T19:00:00.500000Z",
    }


def test_an_attempt_that_never_injected_still_publishes_a_spec(tmp_path):
    benchmark_main = _load_main_module()

    path = benchmark_main._write_fault_spec(tmp_path, SimpleNamespace(fault_spec=None), "test_fault")

    assert json.loads(path.read_text()) == {
        "problem_id": "test_fault",
        "namespace": None,
        "root_cause": None,
        "fault_injected_at": None,
    }


def test_an_unwritable_directory_does_not_fail_the_attempt(tmp_path):
    benchmark_main = _load_main_module()

    assert benchmark_main._write_fault_spec(tmp_path / "missing", SimpleNamespace(fault_spec=None), "x") is None


@pytest.mark.parametrize("existing", ["absent", "file", "symlink", "dangling_symlink", "hardlink", "fifo"])
def test_published_spec_replaces_agent_entries_without_following_them(tmp_path, existing):
    benchmark_main = _load_main_module()
    run = RunArtifacts.create(
        staging_root=tmp_path / "staging",
        results_root=tmp_path / "results",
        problem_id="test_fault",
        agent="codex",
        attempt=1,
    )
    outside = tmp_path / "outside.json"
    outside.write_text('{"sentinel": "keep"}')
    missing = tmp_path / "missing.json"
    path = run.active_dir / "fault-spec.json"
    if existing == "file":
        path.write_text('{"untrusted": true}')
    elif existing == "symlink":
        path.symlink_to(outside)
    elif existing == "dangling_symlink":
        path.symlink_to(missing)
    elif existing == "hardlink":
        os.link(outside, path)
    elif existing == "fifo":
        os.mkfifo(path)

    snapshot = {"problem_id": "test_fault", "attempt": 1}
    published = run.finalize_and_publish(snapshot=snapshot, fieldnames=list(snapshot))
    if existing == "fifo":
        # Bound the regression: opening the existing FIFO for writing would hang.
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import main, sys; from pathlib import Path; from types import SimpleNamespace; "
                "assert main._write_fault_spec(Path(sys.argv[1]), SimpleNamespace(fault_spec=None), 'test_fault')",
                str(published),
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            timeout=15,
        )
    else:
        assert benchmark_main._write_fault_spec(published, SimpleNamespace(fault_spec=None), "test_fault")

    path = published / "fault-spec.json"
    assert stat.S_ISREG(path.lstat().st_mode)
    assert json.loads(path.read_text())["problem_id"] == "test_fault"
    assert outside.read_text() == '{"sentinel": "keep"}'
    assert not missing.exists()
    assert not list(published.glob(".fault-spec.json.sregym-*.tmp"))


@pytest.mark.parametrize("failure", ["write", "replace", "directory"])
def test_failed_spec_write_preserves_destination_and_removes_temporary_file(tmp_path, monkeypatch, failure):
    benchmark_main = _load_main_module()
    path = tmp_path / "fault-spec.json"
    if failure == "directory":
        path.mkdir()
    else:
        path.write_text("keep")

    original_write = Path.write_text

    def fail_write(path, *args, **kwargs):
        original_write(path, "partial")
        raise OSError("write unavailable")

    def fail_replace(*args, **kwargs):
        raise OSError("replace unavailable")

    if failure == "write":
        monkeypatch.setattr(Path, "write_text", fail_write)
    elif failure == "replace":
        monkeypatch.setattr("sregym.run_artifacts.os.replace", fail_replace)

    assert benchmark_main._write_fault_spec(tmp_path, SimpleNamespace(fault_spec=None), "test_fault") is None
    if failure == "directory":
        assert path.is_dir()
    else:
        assert path.read_text() == "keep"
    assert not list(tmp_path.glob(".fault-spec.json.sregym-*.tmp"))
