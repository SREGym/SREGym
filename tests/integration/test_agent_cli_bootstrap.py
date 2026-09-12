"""Install each supported CLI in an ephemeral agent container; make no model calls.

Requires Docker, the agent base image, and access to the package registries.
Run explicitly with pytest -m integration. No host credentials are mounted.
"""

from pathlib import Path

import pytest

from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("agent", ["claudecode", "codex", "gemini", "opencode", "copilot"])
def test_agent_cli_installs_and_reports_version(agent, monkeypatch, tmp_path):
    runner = ContainerRunner(ContainerConfig(memory="2g", cpus=2))
    monkeypatch.setattr(runner, "API_KEY_VARS", [])
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    try:
        result = runner.run_sync(
            ExecInput(
                command=f"bash /opt/sregym/install-scripts/install-{agent}.sh",
                env={"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
                timeout=600,
                label=f"bootstrap-{agent}",
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "installed:" in result.stdout, result.stdout
        print(result.stdout)
    finally:
        runner.cleanup_egress_proxy()
        runner.cleanup_credential_tmps()
