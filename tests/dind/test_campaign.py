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
    monkeypatch.setattr(campaign, "result_csvs", Mock(return_value=[Path("fixture_results.csv")]))
    assert campaign.run_problem("fixture", arguments)["exit"] == 0
    assert all(not path.exists() for path in copies)
