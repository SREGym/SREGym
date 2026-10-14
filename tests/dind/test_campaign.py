import csv
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "dind_campaign", Path(__file__).resolve().parents[2] / "docker/dind/campaign.py"
)
campaign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(campaign)


@pytest.fixture
def arguments(tmp_path, monkeypatch):
    auth = tmp_path / "source-auth.json"
    auth.write_text("dummy credential")
    monkeypatch.setattr(campaign, "AUTH", auth)
    return SimpleNamespace(
        out=tmp_path / "results",
        suffix="",
        retries=0,
        name_prefix="test",
        image="fixture:local",
        cpus=1,
        memory="1g",
        registry_mirrors="",
        env=[],
        model="fixture",
        attempts=1,
        extra=[],
        max_containers_file=None,
    )


@pytest.mark.parametrize("error", [OSError("launch failed"), subprocess.TimeoutExpired("fixture", 1)])
def test_failed_launch_removes_private_auth_copy(arguments, monkeypatch, error):
    copies = []

    def attempt(problem, args, directory, credentials, name):
        copies.append(credentials)
        assert credentials.exists()
        assert credentials.stat().st_mode & 0o777 == 0o600
        assert credentials.parent.stat().st_mode & 0o777 == 0o700
        assert args.out not in credentials.parents
        raise error

    monkeypatch.setattr(campaign, "_run_attempt", attempt)
    with pytest.raises(type(error)):
        campaign.run_problem("fixture", arguments)
    assert all(not path.exists() for path in copies)
    assert not list(arguments.out.rglob("*auth*.json"))
    assert campaign.AUTH.read_text() == "dummy credential"


def test_successful_launch_also_removes_private_auth_copy(arguments, monkeypatch):
    copies = []

    def attempt(problem, args, directory, credentials, name):
        copies.append(credentials)
        return 0, 1.0

    monkeypatch.setattr(campaign, "_run_attempt", attempt)
    monkeypatch.setattr(campaign, "complete_attempts", Mock(return_value={1}))
    assert campaign.run_problem("fixture", arguments)["exit"] == 0
    assert all(not path.exists() for path in copies)


def write_results(directory, rows):
    path = directory / "fixture_results.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["problem_id", "attempt", "run_status"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_incomplete_results_retry_without_reusing_stale_csv(arguments, monkeypatch):
    arguments.attempts = 3
    arguments.retries = 1
    arguments.suffix = ".topup"
    old = arguments.out / "fixture.topup"
    old.mkdir(parents=True)
    stale = write_results(old, [{"problem_id": "fixture", "attempt": 1, "run_status": "complete"}])
    directories, names = [], []

    def attempt(problem, args, directory, credentials, name):
        directories.append(directory)
        names.append(name)
        count = 1 if len(directories) == 1 else 3
        write_results(
            directory, [{"problem_id": problem, "attempt": i, "run_status": "complete"} for i in range(1, count + 1)]
        )
        return 0, 1

    monkeypatch.setattr(campaign, "_run_attempt", attempt)
    result = campaign.run_problem("fixture", arguments)
    assert result["success"] is True
    assert [run["complete_attempts"] for run in result["runs"]] == [1, 3]
    assert len(set(directories)) == len(set(names)) == 2
    assert all(directory.parent == old for directory in directories)
    assert stale.exists()


@pytest.mark.parametrize("status,code", [("incomplete", 0), ("complete", 1)])
def test_incomplete_rows_and_failed_processes_are_not_success(arguments, monkeypatch, status, code):
    def attempt(problem, args, directory, credentials, name):
        write_results(directory, [{"problem_id": problem, "attempt": 1, "run_status": status}])
        return code, 1

    monkeypatch.setattr(campaign, "_run_attempt", attempt)
    result = campaign.run_problem("fixture", arguments)
    assert result["success"] is False


def test_duplicate_exports_do_not_double_count_attempts(tmp_path):
    rows = [{"problem_id": "fixture", "attempt": 1, "run_status": "complete"}]
    first = write_results(tmp_path, rows)
    second = tmp_path / "all_results.csv"
    second.write_bytes(first.read_bytes())
    assert campaign.complete_attempts([first, second], "fixture") == {1}


def test_failed_campaign_returns_nonzero(arguments, monkeypatch):
    monkeypatch.setattr(campaign, "run_problem", lambda *_: {"problem": "fixture", "success": False})
    monkeypatch.setattr(campaign.time, "sleep", lambda _: None)
    monkeypatch.setattr(campaign.sys, "argv", ["campaign.py", "--out", str(arguments.out), "fixture"])
    assert campaign.main() == 1
