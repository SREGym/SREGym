import subprocess
from unittest.mock import Mock

import pytest

from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput
from sregym.service.internet_policy import EndpointRule, InternetPolicy


def test_trusted_container_launch_pull_and_stop_use_same_engine(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///workload.sock")
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(
        ContainerConfig(
            docker_host="unix:///trusted.sock",
            isolate_mounts=False,
            codex_auth="none",
            internet_policy=InternetPolicy.from_mode("open"),
        )
    )
    runner.ensure_image_exists()
    assert run.call_args.args[0][:3] == ["docker", "--host", "unix:///trusted.sock"]
    command = runner.build_docker_command(ExecInput(command="true"))
    assert command[:4] == ["docker", "--host", "unix:///trusted.sock", "run"]
    runner.stop_container("judge", docker_host=runner.config.docker_host)
    assert run.call_args.args[0][:4] == ["docker", "--host", "unix:///trusted.sock", "stop"]


def test_rootless_mounts_do_not_change_trusted_container_config(monkeypatch, tmp_path):
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    monkeypatch.setenv("DOCKER_HOST", "unix:///workload.sock")
    monkeypatch.setenv("SREGYM_RUNNER_ADDRESS", "192.0.2.10")
    config = ContainerConfig(logs_path=tmp_path, codex_auth="none", internet_policy=InternetPolicy.from_mode("open"))
    runner = ContainerRunner(config)
    rewrite = Mock(side_effect=lambda args, outputs: args)
    runner._volumes.rewrite = rewrite
    command = runner.build_docker_command(ExecInput(command="true", env={"AGENT_API_BASE": "http://127.0.0.1:9000/v1"}))
    assert "--add-host=host.docker.internal:192.0.2.10" in command
    assert "AGENT_API_BASE=http://host.docker.internal:9000/v1" in command
    assert rewrite.call_args.args[1] == {tmp_path.resolve()}
    assert ContainerConfig(isolate_mounts=False).isolate_mounts is False


@pytest.mark.parametrize("mode", ["open", "filtered"])
def test_rootless_mcp_routing_and_egress_use_the_active_port_without_host_credentials(monkeypatch, mode):
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    monkeypatch.setenv("DOCKER_HOST", "unix:///workload.sock")
    monkeypatch.setenv("MCP_SERVER_PORT", "24944")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    runner = ContainerRunner(
        ContainerConfig(
            forward_host_credentials=False, codex_auth="none", internet_policy=InternetPolicy.from_mode(mode)
        )
    )
    environment = runner._build_env_vars()
    assert environment["MCP_SERVER_PORT"] == "24944"
    assert environment["MCP_SERVER_URL"] == "http://host.docker.internal:24944"
    assert "OPENAI_API_KEY" not in environment
    rules = runner._configured_egress_rules(environment)
    assert EndpointRule("host.docker.internal", 24944, inspect_tools=False) in rules
    assert EndpointRule("host.docker.internal", 9954, inspect_tools=False) not in rules


def isolated_config(**kwargs):
    return ContainerConfig(docker_host="unix:///chosen.sock", isolate_mounts=True, codex_auth="none", **kwargs)


def test_missing_workload_endpoint_fails_before_startup(monkeypatch):
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    with pytest.raises(ValueError, match="explicit Docker endpoint"):
        ContainerRunner(ContainerConfig(codex_auth="none"))


def test_explicit_isolated_endpoint_controls_proxy_routing(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///ambient.sock")
    monkeypatch.delenv("SREGYM_ROOTLESS_WORKLOAD", raising=False)
    monkeypatch.setenv("SREGYM_RUNNER_ADDRESS", "192.0.2.10")
    runner = ContainerRunner(isolated_config(internet_policy=InternetPolicy.from_mode("filtered")))
    assert runner._egress.docker_host == "unix:///chosen.sock"
    assert runner._egress.isolate_mounts is True
    assert runner._egress._host_mapping() == "--add-host=host.docker.internal:192.0.2.10"


@pytest.mark.parametrize("route", ["sync", "async"])
def test_command_construction_failure_releases_partial_resources(monkeypatch, route):
    runner = ContainerRunner(isolated_config())
    runner.build_docker_command = Mock(side_effect=RuntimeError("construction failed"))
    runner._volumes.close = Mock()
    runner.cleanup_egress_proxy = Mock()
    runner.cleanup_agent_tools = Mock()
    with pytest.raises(RuntimeError, match="construction failed"):
        (runner.run_sync if route == "sync" else runner.run_async)(ExecInput(command="true"))
    runner._volumes.close.assert_called_once()
    runner.cleanup_egress_proxy.assert_called_once()
    runner.cleanup_agent_tools.assert_called_once()


def test_failed_async_launch_stops_owned_name_before_collecting(monkeypatch):
    runner = ContainerRunner(isolated_config())
    request = ExecInput(command="true", container_name="owned-agent")
    runner.build_docker_command = Mock(return_value=["docker", "run"])
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=OSError("launch failed")))
    events = []
    runner.stop_container = Mock(side_effect=lambda *args, **kwargs: events.append(("stop", args, kwargs)))
    runner._volumes.close = Mock(side_effect=lambda **kwargs: events.append(("collect", kwargs)))
    with pytest.raises(OSError, match="launch failed"):
        runner.run_async(request)
    assert events[0][0] == "stop" and events[0][2]["docker_host"] == "unix:///chosen.sock"
    assert events[1][0] == "collect"
    assert not runner._owned_containers


def test_close_retains_failed_owned_stop_and_retries_before_collection():
    runner = ContainerRunner(isolated_config())
    runner._owned_containers["owned-agent"] = "unix:///chosen.sock"
    runner.stop_container = Mock(side_effect=RuntimeError("removal failed"))
    runner._volumes.close = Mock()
    with pytest.raises(ExceptionGroup):
        runner.close()
    assert runner._owned_containers == {"owned-agent": "unix:///chosen.sock"}
    runner._volumes.close.assert_called_once_with(collect=False)
    runner.stop_container = Mock()
    runner.close()
    assert not runner._owned_containers
    runner.stop_container.assert_called_once_with(
        "owned-agent", timeout=5, docker_host="unix:///chosen.sock", strict=True
    )
    assert runner._volumes.close.call_args.kwargs == {"collect": True}


@pytest.mark.parametrize("removed", [True, False])
def test_nonzero_stop_forces_removal_and_reports_unresolved_cleanup(monkeypatch, removed):
    responses = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0 if removed else 1)]
    if not removed:
        responses.append(subprocess.CompletedProcess([], 0, stdout="owned-agent\n"))
    run = Mock(side_effect=responses)
    monkeypatch.setattr(subprocess, "run", run)
    if removed:
        ContainerRunner.stop_container("owned-agent", docker_host="unix:///chosen.sock", strict=True)
    else:
        with pytest.raises(RuntimeError, match="removal failed"):
            ContainerRunner.stop_container("owned-agent", docker_host="unix:///chosen.sock", strict=True)
    assert run.call_args_list[1].args[0] == ["docker", "--host", "unix:///chosen.sock", "rm", "-f", "owned-agent"]


@pytest.mark.parametrize("query_succeeds", [True, False])
def test_auto_removed_owned_container_requires_successful_absence_query(monkeypatch, query_succeeds):
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0 if query_succeeds else 1, stdout=""),
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)
    if query_succeeds:
        ContainerRunner.stop_container("owned-agent", docker_host="unix:///chosen.sock", strict=True)
    else:
        with pytest.raises(RuntimeError, match="removal failed"):
            ContainerRunner.stop_container("owned-agent", docker_host="unix:///chosen.sock", strict=True)
    assert run.call_args.args[0] == [
        "docker",
        "--host",
        "unix:///chosen.sock",
        "container",
        "ls",
        "--all",
        "--filter",
        "name=^/owned\\-agent$",
        "--format",
        "{{.Names}}",
    ]


def test_subscription_copy_has_writable_private_home_without_host_output(monkeypatch, tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text("selected auth")
    (home / "private-sibling").write_text("private state")
    monkeypatch.setenv("CODEX_HOME", str(home))
    runner = ContainerRunner(ContainerConfig(docker_host="unix:///chosen.sock", isolate_mounts=True, codex_auth="copy"))
    args = []
    runner._mount_codex_credentials(args)
    runner._volumes._run = Mock()
    copied = next(iter(runner._writable_credential_inputs))
    assert copied.read_text() == "selected auth"
    assert not (copied.parent / "private-sibling").exists()
    rewritten = runner._volumes.rewrite(args, set(), writable_inputs=runner._writable_credential_inputs)
    assert len(runner._volumes.volumes) == 1 and runner._volumes.volumes[0][1] is None
    assert any("dst=/root/.codex,volume-nocopy" in item and "readonly" not in item for item in rewritten)
    runner._volumes.close(collect=False)
    runner.cleanup_credential_tmps()
    assert (home / "auth.json").read_text() == "selected auth"
    assert (home / "private-sibling").read_text() == "private state"


@pytest.mark.parametrize("isolated", [True, False])
def test_shared_host_refresh_is_rejected_only_for_isolated_agents(monkeypatch, tmp_path, isolated):
    (tmp_path / "auth.json").write_text("selected auth")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    runner = ContainerRunner(
        ContainerConfig(docker_host="unix:///chosen.sock", isolate_mounts=isolated, codex_auth="shared")
    )
    args = []
    if isolated:
        with pytest.raises(ValueError, match="trusted, non-isolated"):
            runner._mount_codex_credentials(args)
        assert not args
    else:
        runner._mount_codex_credentials(args)
        assert args == ["-v", f"{(tmp_path / 'auth.json').resolve()}:/root/.codex/auth.json:rw"]


def test_uncertain_tools_volume_creation_is_owned_and_cleaned():
    runner = ContainerRunner(isolated_config(internet_policy=InternetPolicy.from_mode("filtered")))
    runner._run_docker_checked = Mock(side_effect=[TimeoutError("response lost"), None])
    with pytest.raises(TimeoutError, match="response lost"):
        runner.prepare_agent_tools("install-codex.sh", "1.0")
    calls = runner._run_docker_checked.call_args_list
    created = calls[0].args[0][-1]
    assert calls[1].args[0] == ["docker", "--host", "unix:///chosen.sock", "volume", "rm", "-f", created]
    assert runner._agent_tools_volume is None


def test_failed_tools_cleanup_is_retryable():
    runner = ContainerRunner(isolated_config())
    runner._agent_tools_volume = "owned-tools"
    runner._run_docker_checked = Mock(side_effect=RuntimeError("removal failed"))
    with pytest.raises(RuntimeError, match="removal failed"):
        runner.cleanup_agent_tools()
    assert runner._agent_tools_volume == "owned-tools"
    runner._run_docker_checked = Mock()
    runner.cleanup_agent_tools()
    assert runner._agent_tools_volume is None


def test_installer_launch_failure_stops_owned_name_on_selected_endpoint(monkeypatch):
    runner = ContainerRunner(isolated_config(internet_policy=InternetPolicy.from_mode("filtered")))
    runner._run_docker_checked = Mock()
    runner.stop_container = Mock()
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=OSError("installer failed")))
    with pytest.raises(OSError, match="installer failed"):
        runner.prepare_agent_tools("install-codex.sh", "1.0")
    assert runner.stop_container.call_args.kwargs == {
        "timeout": 5,
        "docker_host": "unix:///chosen.sock",
        "strict": True,
    }
    assert runner.stop_container.call_args.args[0].startswith("agent-tool-setup-")
    assert not runner._owned_containers and runner._agent_tools_volume is None


def test_egress_failed_removals_retain_names_and_state_for_retry(monkeypatch, tmp_path):
    runner = ContainerRunner(isolated_config())
    proxy = runner._egress
    state = tmp_path / "owned-state"
    state.mkdir()
    proxy._proxy_name, proxy._network_name, proxy._tmp_dir = "owned-proxy", "owned-network", state
    run = Mock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="daemon unavailable"))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ExceptionGroup):
        proxy.close()
    assert run.call_count == 2
    assert proxy._proxy_name == "owned-proxy" and proxy._network_name == "owned-network" and state.exists()
    run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    proxy.close()
    assert proxy._proxy_name is None and proxy._network_name is None and proxy._tmp_dir is None
    assert not state.exists()
    assert run.call_args.args[0] == [
        "docker",
        "--host",
        "unix:///chosen.sock",
        "network",
        "rm",
        "--force",
        "owned-network",
    ]
