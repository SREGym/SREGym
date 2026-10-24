import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import AgentRegistration
from sregym.service.container_runner import ContainerConfig, ContainerRunner
from sregym.service.docker_egress import DockerEgress
from sregym.service.internet_policy import EndpointRule, InternetPolicy


@pytest.mark.parametrize("failed_step", ["state", "network", "proxy", "directories", "copy", "connect", "certificate"])
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
        current = step.get(tuple(command[1:3]))
        if command[1] == "exec" and command[3] == "mkdir":
            current = "directories"
        elif command[1] == "cp":
            current = "copy"
        failed = current == failed_step
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
    assert any(call.args[0][1:3] == ["volume", "rm"] for call in run.call_args_list)
    if failed_step == "directories":
        assert not any(
            call.args[0][1] == "cp" and call.args[0][-1].endswith(":/addons/egress_proxy.py")
            for call in run.call_args_list
        )
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


def test_proxy_bootstrap_uses_docker_copy_instead_of_host_bind_mounts(monkeypatch):
    egress = DockerEgress(k8s_proxy_port=17443)
    commands = []
    directories_ready = False

    def docker(command, **kwargs):
        nonlocal directories_ready
        commands.append(command)
        if command[1] == "exec" and command[3:] == ["mkdir", "-p", "/addons", "/state"]:
            assert command[2] == egress._proxy_name
            assert any(previous[1:3] == ["run", "-d"] for previous in commands)
            directories_ready = True
        if command[1] == "cp" and command[-1].startswith(f"{egress._proxy_name}:"):
            # The detached bootstrap need not have run before the first copy.
            assert directories_ready, "copy destination is missing"
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(egress, "_ensure_proxy_image_exists", lambda: None)
    monkeypatch.setattr(egress, "_copy_proxy_certificate", lambda: None)
    monkeypatch.setattr("sregym.service.docker_egress.subprocess.run", docker)

    egress.ensure_started((EndpointRule("provider.test", 443),))

    proxy_command = next(command for command in commands if command[1:3] == ["run", "-d"])
    assert f"{egress._cert_volume}:/certs" in proxy_command
    assert str(egress._tmp_dir) not in proxy_command
    assert proxy_command[-3:-1] == ["sh", "-ec"]
    assert "ignore_hosts=^host\\.docker\\.internal:17443$" in proxy_command[-1]
    copy_commands = [command for command in commands if command[1] == "cp"]
    assert len(copy_commands) == 3
    assert all(command[-1].startswith(f"{egress._proxy_name}:") for command in copy_commands)
    assert [
        "docker",
        "exec",
        egress._proxy_name,
        "touch",
        "/state/blocked-requests.jsonl",
        "/state/.sregym-proxy-ready",
    ] in commands
    egress.close()


def test_blocked_request_audit_reads_the_proxy_state(monkeypatch):
    egress = DockerEgress()
    egress._proxy_name = "test-proxy"
    record = {
        "timestamp": "2026-10-16T00:00:00+00:00",
        "method": "CONNECT",
        "scheme": "https",
        "host": "blocked.example",
        "port": 443,
        "reason": "endpoint-not-allowed",
    }
    monkeypatch.setattr(
        "sregym.service.docker_egress.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(record) + "\n")),
    )

    assert egress.blocked_request_count() == 1
    assert egress.blocked_request_records() == [record]


def test_stopped_proxy_audit_is_copied_instead_of_using_stale_host_log(monkeypatch, tmp_path):
    egress = DockerEgress()
    egress._proxy_name = "stopped-proxy"
    egress._tmp_dir = tmp_path
    (tmp_path / "blocked-requests.jsonl").touch()
    record = {
        "timestamp": "now",
        "method": "CONNECT",
        "scheme": "",
        "host": "blocked.test",
        "port": 443,
        "reason": "endpoint-not-allowed",
    }

    def docker(command, **kwargs):
        assert kwargs["timeout"] == 10
        if command[1] == "cp":
            Path(command[-1]).write_text(json.dumps(record) + "\n")
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="container is not running")

    monkeypatch.setattr("sregym.service.docker_egress.subprocess.run", docker)
    assert egress.blocked_request_records() == [record]
    assert (tmp_path / "blocked-requests.jsonl").read_text() == ""


@pytest.mark.parametrize("failure", ["unavailable", "timeout"])
def test_unreadable_proxy_audit_is_not_replaced_by_empty_host_log(monkeypatch, tmp_path, failure):
    egress = DockerEgress()
    egress._proxy_name = "unavailable-proxy"
    egress._tmp_dir = tmp_path
    (tmp_path / "blocked-requests.jsonl").touch()
    run = Mock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="unavailable"))
    if failure == "timeout":
        run.side_effect = subprocess.TimeoutExpired("docker", 10)
    monkeypatch.setattr("sregym.service.docker_egress.subprocess.run", run)

    with pytest.raises(OSError, match="Could not read"):
        egress.blocked_request_records()


def test_proxy_cleanup_removes_its_anonymous_volumes(monkeypatch):
    egress = DockerEgress()
    egress._proxy_name = "proxy"
    egress._network_name = "network"
    egress._cert_volume = "certificates"
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("sregym.service.docker_egress.subprocess.run", run)

    egress.close()

    assert [call.args[0] for call in run.call_args_list] == [
        ["docker", "rm", "-f", "-v", "proxy"],
        ["docker", "network", "rm", "network"],
        ["docker", "volume", "rm", "certificates"],
    ]


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


@pytest.mark.parametrize(
    "kickoff_env", [None, {"AGENT_API_BASE": "https://custom.test/v1", "AGENT_API_KEY": "test-key"}]
)
def test_preflight_and_run_share_registered_provider_environment(monkeypatch, kickoff_env):
    launcher = AgentLauncher()
    runner = ContainerRunner(
        ContainerConfig(
            internet_policy=InternetPolicy.from_mode("filtered", agent_name="stratus", model_id="anthropic/model")
        )
    )
    launcher._container_runner = runner
    reg = AgentRegistration(name="stratus", kickoff_command="run-agent", kickoff_env=kickoff_env)
    monkeypatch.setattr(runner, "run_sync", Mock(return_value=subprocess.CompletedProcess([], 0)))
    monkeypatch.setattr(runner, "run_async", Mock(return_value=Mock()))
    monkeypatch.setattr(launcher, "_pipe_logs", Mock())
    with monkeypatch.context() as imports:
        imports.setattr("sregym.agent_launcher.importlib.import_module", Mock(return_value=Mock()))
        launcher._run_preflight(reg)
    asyncio.run(launcher._start_containerized(reg))

    preflight = runner.run_sync.call_args.args[0]
    runtime = runner.run_async.call_args.args[0]
    assert preflight.env == (kickoff_env or {})
    assert preflight.env is not kickoff_env
    for name, value in (kickoff_env or {}).items():
        assert runtime.env[name] == value
    preflight_rules = runner._configured_egress_rules(runner._build_env_vars(preflight.env))
    runtime_rules = runner._configured_egress_rules(runner._build_env_vars(runtime.env))
    assert preflight_rules == runtime_rules
    if kickoff_env:
        assert EndpointRule("custom.test", 443) in preflight_rules
        assert "AGENT_LOGS_DIR" not in kickoff_env


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
