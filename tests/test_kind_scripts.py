import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="KIND setup requires a Unix shell")
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def shell_environment(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "kind"
    scripts.mkdir(parents=True)
    (scripts / "setup_kind_cluster.sh").write_text((REPO_ROOT / "kind/setup_kind_cluster.sh").read_text())
    (scripts / "kind-config.yaml").touch()
    commands = tmp_path / "bin"
    commands.mkdir()
    for command in ("kind", "kubectl", "rm"):
        executable = commands / command
        executable.write_text(
            f'#!/bin/sh\ncommand_name={command}\nprintf "%s %s\\n" "$command_name" "$*" >> "$KIND_TEST_CALL_LOG"\n'
        )
        executable.chmod(0o755)
    log = tmp_path / "calls.txt"
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "KIND_TEST_CALL_LOG": str(log),
    }

    def run(script, *args, extra_env=None):
        result = subprocess.run(
            ["/bin/bash", str(scripts / script), *args],
            env={**env, **(extra_env or {})},
            capture_output=True,
            text=True,
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    return run


@pytest.mark.parametrize("args", [(), ("auto",), ("arm",), ("x86",)])
def test_kind_setup_uses_shared_config_for_default_and_legacy_aliases(shell_environment, args):
    result, calls = shell_environment("setup_kind_cluster.sh", *args)
    assert result.returncode == 0, result.stderr
    assert calls[0].startswith("kind create cluster --config ")
    assert calls[0].endswith("kind-config.yaml")
    assert any("kubectl wait --for=condition=Ready nodes --all" in call for call in calls)


def test_unknown_argument_fails_before_creating_cluster(shell_environment):
    result, calls = shell_environment("setup_kind_cluster.sh", "unknown")
    assert result.returncode != 0
    assert not calls


def test_kind_setup_accepts_dind_config_and_node_image(shell_environment, tmp_path):
    config = tmp_path / "dind-kind.yaml"
    config.touch()
    result, calls = shell_environment(
        "setup_kind_cluster.sh",
        "arm",
        extra_env={
            "KIND_CONFIG": str(config),
            "KIND_NODE_IMAGE": "sregym-kind:local",
            "KIND_RETAIN_ON_FAILURE": "true",
        },
    )
    assert result.returncode == 0, result.stderr
    assert calls[0] == f"kind create cluster --config {config} --image sregym-kind:local --retain"
