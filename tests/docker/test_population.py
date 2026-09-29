import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def population_check():
    spec = importlib.util.spec_from_file_location("population_check", ROOT / "docker/flight-ticket/test_population.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_population_build_preserves_original_runtime_dependencies():
    directory = ROOT / "docker/flight-ticket"
    requirements = {
        line
        for line in (directory / "populate-redis-requirements.txt").read_text().splitlines()
        if line and not line.startswith("#")
    }
    assert requirements == {
        "redis==5.2.1",
        "pandas==2.2.3",
        "numpy==2.2.1",
        "python-dateutil==2.9.0.post0",
        "pytz==2024.2",
        "six==1.17.0",
        "tzdata==2024.2",
    }
    recipe = (directory / "populate-redis.Dockerfile").read_text()
    assert "COPY populate-redis-requirements.txt /app/requirements.txt" in recipe
    assert "pip install --no-cache-dir -r requirements.txt" in recipe


@pytest.mark.parametrize("failure", [False, True])
def test_population_check_exercises_selected_image_and_cleans_up(population_check, monkeypatch, failure):
    docker = Mock(return_value="PONG")
    worker = Mock(side_effect=subprocess.CalledProcessError(1, "docker") if failure else None)
    monkeypatch.setattr(population_check, "docker", docker)
    monkeypatch.setattr(population_check.subprocess, "run", worker)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            population_check.check_population("registry.test/populate:v1", "arm64")
    else:
        population_check.check_population("registry.test/populate:v1", "arm64")
    command = worker.call_args.args[0]
    assert command[command.index("--platform") + 1] == "linux/arm64"
    assert command[-2:] == ["registry.test/populate:v1", "-"]
    assert "subprocess.run([sys.executable" in worker.call_args.kwargs["input"]
    assert docker.call_args_list[0].args[:3] == ("network", "create", "--internal")
    assert docker.call_args_list[-2].args[:3] == ("rm", "-f", "--volumes")
    assert docker.call_args_list[-1].args[:2] == ("network", "rm")


def test_population_check_cleans_up_if_redis_never_starts(population_check, monkeypatch):
    docker = Mock(return_value="")
    worker = Mock()
    monkeypatch.setattr(population_check, "docker", docker)
    monkeypatch.setattr(population_check.time, "sleep", Mock())
    monkeypatch.setattr(population_check.subprocess, "run", worker)
    with pytest.raises(RuntimeError, match="Redis 4 did not become ready"):
        population_check.check_population("registry.test/populate:v1", "amd64")
    worker.assert_not_called()
    assert docker.call_args_list[-2].args[:3] == ("rm", "-f", "--volumes")
    assert docker.call_args_list[-1].args[:2] == ("network", "rm")
