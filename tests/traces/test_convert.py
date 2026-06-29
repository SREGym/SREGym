"""Tests for the conversion dispatch (path parsing, app mapping, boundary)."""

import json
import shutil
from pathlib import Path

import pytest

from sregym.traces import convert
from sregym.traces.atif import Trajectory

FIXTURE_RUN = Path(__file__).parent / "fixtures" / "claudecode_run"


def _canonical_run_dir(tmp_path: Path) -> Path:
    """Materialize the fixture under a canonical results/ path layout."""
    run_dir = tmp_path / "results" / "0629_1125" / "claudecode" / "service_port_conflict_hotel_reservation" / "run_1"
    run_dir.parent.mkdir(parents=True)
    shutil.copytree(FIXTURE_RUN, run_dir)
    return run_dir


def test_convert_run_dispatches_claudecode(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    traj = convert.convert_run(run_dir)
    assert isinstance(traj, Trajectory)
    assert traj.agent.name == "claudecode"
    # Validates (round-trips through the model).
    Trajectory.model_validate(traj.to_json_dict())


def test_extra_sregym_populated(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["problem_id"] == "service_port_conflict_hotel_reservation"
    assert sregym["application"] == "Hotel Reservation"
    assert sregym["run"] == 1
    assert sregym["submitted"] is True
    assert sregym["results_path"].endswith("claudecode/service_port_conflict_hotel_reservation/run_1")


# The fixture submits the diagnosis at step 8 (the first step whose observation
# carries the conductor's {"status":"200","message":"Submission received"}
# envelope; a later step 13 submits the mitigation). Hardcoded so this test
# pins the value independently of the detection algorithm it exercises.
EXPECTED_DIAGNOSIS_STEP = 8


def test_diagnosis_submitted_step(tmp_path):
    run_dir = _canonical_run_dir(tmp_path)
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["diagnosis_submitted_step"] == EXPECTED_DIAGNOSIS_STEP
    # Sanity: that step really does carry the submission envelope.
    step = next(s for s in traj.steps if s.step_id == EXPECTED_DIAGNOSIS_STEP)
    assert step.observation is not None
    blob = json.dumps([r.content for r in step.observation.results], default=str)
    assert "Submission received" in blob


@pytest.mark.parametrize(
    "problem_id,expected",
    [
        ("service_port_conflict_hotel_reservation", "Hotel Reservation"),
        ("duplicate_pvc_mounts_social_network", "Social Network"),
        ("missing_env_variable_astronomy_shop", "Astronomy Shop"),
        ("kubelet_crash", None),
        ("operator_overload_replicas", None),
    ],
)
def test_application_longest_suffix_mapping(problem_id, expected):
    assert convert.map_application(problem_id) == expected


def test_parse_run_path():
    p = Path("/x/results/0629_1125/claudecode/service_port_conflict_hotel_reservation/run_3")
    info = convert.parse_run_path(p)
    assert info.tool == "claudecode"
    assert info.problem_id == "service_port_conflict_hotel_reservation"
    assert info.run == 3
    assert info.batch == "0629_1125"
