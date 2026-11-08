"""Prewarming must retain the exact lock and fail on incomplete dependencies."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sregym.harbor.preload_charts import build


def chart(tmp_path: Path, name: str = "app") -> Path:
    path = tmp_path / name
    path.mkdir()
    (path / "Chart.lock").write_text(
        "dependencies:\n- name: database\n  version: 1.2.3\n  repository: https://charts.example.org\n"
    )
    return path


def listing(status: str = "ok") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        [], 0, f"NAME\tVERSION\tREPOSITORY\tSTATUS\ndatabase\t1.2.3\thttps://charts.example.org\t{status}\n"
    )


def test_locked_build_registers_shared_repository_once(tmp_path):
    paths = [chart(tmp_path, "one"), chart(tmp_path, "two")]
    originals = [(path / "Chart.lock").read_bytes() for path in paths]
    with patch("sregym.harbor.preload_charts.subprocess.run", return_value=listing()) as run:
        build(paths)
    assert sum(call.args[0][1:3] == ["repo", "add"] for call in run.call_args_list) == 1
    builds = [call.args[0] for call in run.call_args_list if call.args[0][1:3] == ["dependency", "build"]]
    assert builds == [["helm", "dependency", "build", "--skip-refresh", str(path)] for path in paths]
    assert [(path / "Chart.lock").read_bytes() for path in paths] == originals


def test_missing_lock_never_resolves_new_versions(tmp_path):
    with patch("sregym.harbor.preload_charts.subprocess.run") as run, pytest.raises(FileNotFoundError):
        build([tmp_path])
    run.assert_not_called()


def test_lock_change_fails_build(tmp_path):
    path = chart(tmp_path)

    def helm(args, **kwargs):
        if args[1:3] == ["dependency", "build"]:
            (path / "Chart.lock").write_text("changed by Helm")
        return listing()

    with (
        patch("sregym.harbor.preload_charts.subprocess.run", side_effect=helm),
        pytest.raises(RuntimeError, match="changed the dependency lock"),
    ):
        build([path])


@pytest.mark.parametrize("status", ["missing", "wrong version", ""])
def test_unsatisfied_dependency_fails(tmp_path, status):
    with (
        patch("sregym.harbor.preload_charts.subprocess.run", return_value=listing(status)),
        pytest.raises(RuntimeError, match="not satisfied"),
    ):
        build([chart(tmp_path)])


def test_registry_errors_propagate(tmp_path):
    with (
        patch("sregym.harbor.preload_charts.subprocess.run", side_effect=subprocess.CalledProcessError(1, "helm")),
        pytest.raises(subprocess.CalledProcessError),
    ):
        build([chart(tmp_path)])
