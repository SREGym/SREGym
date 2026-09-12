import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Image build uses a Unix shell")
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("explicit,expected", [(None, "v1.33.9"), ("v1.32.1", "v1.32.1")])
def test_agent_image_matches_host_client_unless_explicitly_pinned(tmp_path, explicit, expected):
    repo = tmp_path / "repo"
    build_dir = repo / "docker" / "agents"
    build_dir.mkdir(parents=True)
    (build_dir / "build.sh").write_text((REPO_ROOT / "docker" / "agents" / "build.sh").read_text())
    for directory in ("clients", "logger", "llm_backend", "docker/agents/install-scripts"):
        (repo / directory).mkdir(parents=True)
    for path in (
        "sregym/__init__.py",
        "sregym/paths.py",
        "sregym/service/__init__.py",
        "sregym/service/kubectl.py",
        "sregym/service/helm.py",
        "sregym/service/apps/base.py",
        "sregym/service/apps/helpers.py",
        "docker/agents/Dockerfile",
        "docker/agents/requirements-container.txt",
    ):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.txt"
    for command in ("docker", "kubectl"):
        executable = bin_dir / command
        executable.write_text(
            "#!/bin/sh\n"
            f'printf "%s %s\\n" {command} "$*" >> "$SREGYM_BUILD_TEST_LOG"\n'
            + ('printf "clientVersion:\\n  gitVersion: v1.33.9\\n"\n' if command == "kubectl" else "")
        )
        executable.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "SREGYM_BUILD_TEST_LOG": str(log)}
    env.pop("KUBECTL_VERSION", None)
    if explicit:
        env["KUBECTL_VERSION"] = explicit
    result = subprocess.run(["/bin/bash", str(build_dir / "build.sh")], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    build = next(call for call in calls if call.startswith("docker build "))
    assert f"--build-arg KUBECTL_VERSION={expected}" in build
    assert any(call.startswith("kubectl version ") for call in calls) == (explicit is None)
    assert not (build_dir / "build-context").exists()
