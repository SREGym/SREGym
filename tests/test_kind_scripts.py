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
    for architecture in ("arm", "x86"):
        (scripts / f"kind-config-{architecture}.yaml").touch()
    commands = tmp_path / "bin"
    commands.mkdir()
    for command in ("kind", "kubectl", "rm", "uname"):
        executable = commands / command
        executable.write_text(
            "#!/bin/sh\n"
            f"command_name={command}\n"
            'if [ "$command_name" = uname ]; then echo "$KIND_TEST_ARCH"; exit; fi\n'
            'printf "%s %s\\n" "$command_name" "$*" >> "$KIND_TEST_CALL_LOG"\n'
        )
        executable.chmod(0o755)
    log = tmp_path / "calls.txt"
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "KIND_TEST_CALL_LOG": str(log),
        "KIND_TEST_ARCH": "arm64",
    }

    def run(script, *args, architecture="arm64"):
        result = subprocess.run(
            ["/bin/bash", str(scripts / script), *args],
            env={**env, "KIND_TEST_ARCH": architecture},
            capture_output=True,
            text=True,
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    return run


@pytest.mark.parametrize("host,config", [("arm64", "arm"), ("aarch64", "arm"), ("x86_64", "x86")])
def test_kind_setup_selects_native_config(shell_environment, host, config):
    result, calls = shell_environment("setup_kind_cluster.sh", architecture=host)
    assert result.returncode == 0, result.stderr
    assert calls[0].startswith("kind create cluster --config ")
    assert calls[0].endswith(f"kind-config-{config}.yaml")
    assert any("kubectl wait --for=condition=Ready nodes --all" in call for call in calls)


def test_explicit_kind_config_overrides_host_detection(shell_environment):
    result, calls = shell_environment("setup_kind_cluster.sh", "x86", architecture="arm64")
    assert result.returncode == 0
    assert calls[0].endswith("kind-config-x86.yaml")


def test_unsupported_architecture_fails_before_creating_cluster(shell_environment):
    result, calls = shell_environment("setup_kind_cluster.sh", architecture="unknown")
    assert result.returncode != 0
    assert not calls
