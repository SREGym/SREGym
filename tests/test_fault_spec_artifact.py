"""Each published attempt carries the injected fault's ground truth as fault-spec.json."""

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from sregym.conductor.conductor import Conductor


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
