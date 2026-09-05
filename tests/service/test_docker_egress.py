import subprocess
from unittest.mock import Mock

import pytest

from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import AgentRegistration
from sregym.service.container_runner import ContainerRunner
from sregym.service.docker_egress import DockerEgress
from sregym.service.internet_policy import EndpointRule


@pytest.mark.parametrize("failed_step", ["state", "network", "proxy", "connect", "certificate"])
def test_startup_failure_releases_all_acquired_resources(monkeypatch, failed_step):
    egress = DockerEgress()
    paths = []
    prepare_state = egress._prepare_state

    def prepare():
        prepare_state()
        paths.append(egress._tmp_dir)
        if failed_step == "state":
            raise RuntimeError("failed at state")

    def docker(command, **kwargs):
        step = {("network", "create"): "network", ("run", "-d"): "proxy", ("network", "connect"): "connect"}
        failed = step.get(tuple(command[1:3])) == failed_step
        return subprocess.CompletedProcess(command, int(failed), stdout="true", stderr="test failure" if failed else "")

    def certificate():
        if failed_step == "certificate":
            raise RuntimeError("failed at certificate")

    monkeypatch.setattr(egress, "_prepare_state", prepare)
    monkeypatch.setattr(egress, "_copy_proxy_certificate", certificate)
    run = Mock(side_effect=docker)
    monkeypatch.setattr("sregym.service.docker_egress.subprocess.run", run)

    with pytest.raises(RuntimeError):
        egress.ensure_started((EndpointRule("provider.test", 443),))

    assert paths and all(not path.exists() for path in paths)
    assert any(call.args[0][1:3] == ["rm", "-f"] for call in run.call_args_list)
    assert any(call.args[0][1:3] == ["network", "rm"] for call in run.call_args_list)
    with pytest.raises(RuntimeError, match="not ready"):
        egress.docker_args()
    count = run.call_count
    egress.close()
    assert run.call_count == count


def test_reuse_checks_the_proxy_and_never_reopens_a_stopped_proxy(monkeypatch):
    egress = DockerEgress()
    egress._proxy_name = "existing-proxy"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="true"))
    monkeypatch.setattr("sregym.service.docker_egress.subprocess.run", run)
    egress.ensure_started(())
    assert run.call_count == 1
    assert run.call_args.args[0][1] == "inspect"
    run.return_value.stdout = "false"
    with pytest.raises(RuntimeError, match="refusing to start an unrestricted agent"):
        egress.ensure_started(())


def test_runner_cleanup_attempts_each_resource_even_if_proxy_cleanup_fails(monkeypatch):
    runner = ContainerRunner()
    monkeypatch.setattr(runner._egress, "close", Mock(side_effect=RuntimeError("docker unavailable")))
    credentials = Mock()
    tools = Mock()
    monkeypatch.setattr(runner, "cleanup_credential_tmps", credentials)
    monkeypatch.setattr(runner, "cleanup_agent_tools", tools)
    with pytest.raises(RuntimeError, match="docker unavailable"):
        runner.close()
    credentials.assert_called_once()
    tools.assert_called_once()


@pytest.mark.parametrize("prepared", [True, False])
@pytest.mark.parametrize("capture_logs", [True, False])
def test_preflight_and_run_share_the_install_decision(prepared, capture_logs):
    runner = ContainerRunner()
    runner._agent_tools_volume = "prepared-tools" if prepared else None
    command = runner.build_composite_command("install-codex.sh", "1.2.3", "run-agent", capture_logs=capture_logs)
    assert ("install-codex.sh" in command) is not prepared
    assert ("/logs/driver.log" in command) is capture_logs
    assert "run-agent" in command


@pytest.mark.parametrize("failure", ["installation", "preflight", "interrupt"])
def test_launcher_preparation_failure_closes_runner(monkeypatch, failure):
    launcher = AgentLauncher()
    runner = ContainerRunner()
    launcher._container_runner = runner
    monkeypatch.setattr(runner, "close", Mock())
    monkeypatch.setattr(runner, "prepare_agent_tools", Mock())
    monkeypatch.setattr(launcher, "_run_preflight", Mock())
    if failure == "installation":
        runner.prepare_agent_tools.side_effect = RuntimeError("installation failed")
    elif failure == "preflight":
        launcher._run_preflight.side_effect = SystemExit(1)
    else:
        launcher._run_preflight.side_effect = KeyboardInterrupt()

    with pytest.raises((RuntimeError, SystemExit, KeyboardInterrupt)):
        launcher.prepare_agent(AgentRegistration(name="codex"))
    runner.close.assert_called_once()
    assert launcher._container_runner is None
