"""The MCP port-forward reclaim must only ever kill processes we started.

`_kill_stale_port_forward` exists to clear a port-forward leaked by an earlier
run. It finds the holder with `lsof`, which reports whatever owns the port --
including services that have nothing to do with SREGym. Killing on that alone
takes out unrelated processes on a developer's machine, so ownership is decided
by the command line rather than by port occupancy.
"""

import re
import subprocess
from types import SimpleNamespace

import pytest

from sregym.agent_launcher import AgentLauncher
from sregym.conductor.conductor import ConductorConfig
from sregym.service.container_runner import ContainerRunner
from sregym.service.internet_policy import InternetPolicy
from sregym.service.mcp_server import MCPServer

OWN_CMDLINE = "kubectl port-forward svc/mcp-server 9954:9954 -n sregym --address 0.0.0.0"


@pytest.fixture
def server(monkeypatch):
    # __init__ builds a KubeCtl, which we neither need nor want here.
    monkeypatch.setattr("sregym.service.mcp_server.KubeCtl", lambda: None)
    return MCPServer()


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (OWN_CMDLINE, True),
        # Another service on the port: the case that must never be killed.
        ("/usr/bin/python3 -m http.server 9954", False),
        ("mkdocs serve", False),
        # A port-forward, but to something that isn't ours.
        ("kubectl port-forward svc/grafana 9954:3000 -n observe", False),
        # Unreadable command line (hidepid, vanished process) reads as foreign,
        # so the failure mode is a reported conflict rather than a stray kill.
        ("", False),
    ],
)
def test_ownership_is_decided_by_cmdline(server, monkeypatch, cmdline, expected):
    monkeypatch.setattr(server, "_process_cmdline", lambda _pid: cmdline)
    assert server._is_own_port_forward("1234") is expected


def test_foreign_holder_is_left_alone(server, monkeypatch):
    monkeypatch.setattr(server, "is_port_in_use", lambda _port: True)
    monkeypatch.setattr(server, "_process_cmdline", lambda _pid: "mkdocs serve")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="4242\n", stderr="")
    )
    killed = []
    monkeypatch.setattr("sregym.service.mcp_server.os.kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr("sregym.service.mcp_server.time.sleep", lambda _s: None)

    server._kill_stale_port_forward()

    assert killed == []


def test_own_leaked_port_forward_is_reclaimed(server, monkeypatch):
    monkeypatch.setattr(server, "is_port_in_use", lambda _port: True)
    monkeypatch.setattr(server, "_process_cmdline", lambda _pid: OWN_CMDLINE)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="4242\n", stderr="")
    )
    killed = []
    monkeypatch.setattr("sregym.service.mcp_server.os.kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr("sregym.service.mcp_server.time.sleep", lambda _s: None)

    server._kill_stale_port_forward()

    assert killed == [4242]


def test_a_vanished_pid_does_not_abort_the_remaining_holders(server, monkeypatch):
    """A holder can exit between `lsof` and the signal.

    os.kill raises ProcessLookupError there, and letting it reach the outer
    handler would skip every holder after it.
    """
    monkeypatch.setattr(server, "is_port_in_use", lambda _port: True)
    monkeypatch.setattr(server, "_process_cmdline", lambda _pid: OWN_CMDLINE)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="4242\n4243\n", stderr="")
    )
    killed = []

    def flaky_kill(pid, _sig):
        if pid == 4242:
            raise ProcessLookupError(pid)
        killed.append(pid)

    monkeypatch.setattr("sregym.service.mcp_server.os.kill", flaky_kill)
    monkeypatch.setattr("sregym.service.mcp_server.time.sleep", lambda _s: None)

    server._kill_stale_port_forward()

    assert killed == [4243]


def test_process_cmdline_reads_the_current_process(server):
    """Exercises whichever source this platform actually has.

    procfs on Linux, `ps` on macOS -- and `ps` is absent from slim container
    images, so neither branch is redundant. Asserting only that a command line
    comes back keeps this independent of how the suite was invoked.
    """
    import os

    assert server._process_cmdline(str(os.getpid())).strip()
    # A pid that cannot exist yields "", which _is_own_port_forward treats as
    # foreign.
    assert server._process_cmdline("999999").strip() == ""


def test_mcp_port_is_overridable(monkeypatch):
    monkeypatch.setattr("sregym.service.mcp_server.KubeCtl", lambda: None)
    monkeypatch.delenv("MCP_SERVER_PORT", raising=False)
    assert MCPServer().port == 9954
    monkeypatch.setenv("MCP_SERVER_PORT", "9970")
    assert MCPServer().port == 9970


def test_k8s_proxy_port_is_overridable():
    assert ConductorConfig().k8s_proxy_listen_port == 16443
    assert ConductorConfig(k8s_proxy_listen_port=16500).k8s_proxy_listen_port == 16500


@pytest.mark.parametrize("port", [16443, 17443])
def test_launcher_passes_k8s_port_to_egress_proxy(monkeypatch, tmp_path, port):
    monkeypatch.setattr(ContainerRunner, "ensure_image_exists", lambda self: None)
    launcher = AgentLauncher()
    launcher.set_internet_policy(InternetPolicy.from_mode("filtered"))
    launcher.enable_container_isolation(k8s_proxy_port=port)
    runner = launcher._container_runner
    assert runner is not None
    assert runner.config.k8s_proxy_port == port

    commands = []
    monkeypatch.setattr(runner, "_ensure_proxy_image_exists", lambda: None)
    monkeypatch.setattr(runner, "_prepare_egress_state", lambda: None)
    monkeypatch.setattr(runner, "_copy_proxy_certificate", lambda: None)
    monkeypatch.setattr(runner, "_run_docker_checked", lambda command, action: commands.append(command))
    runner._egress_tmp_dir = tmp_path
    runner._ensure_filtered_egress()

    proxy_command = next(command for command in commands if "mitmdump" in command)
    rule = next(arg.removeprefix("ignore_hosts=") for arg in proxy_command if arg.startswith("ignore_hosts="))
    assert re.search(rule, f"host.docker.internal:{port}")
    assert not re.search(rule, f"host.docker.internal:{port + 1}")
    assert not re.search(rule, f"host.docker.internal:{port}0")
    assert not re.search(rule, f"other.example:{port}")
    assert not re.search(rule, f"hostXdockerXinternal:{port}")


def test_main_sets_custom_k8s_port_before_agent_preflight(monkeypatch):
    import main as benchmark

    class StopBeforeDeployment(Exception):
        pass

    monkeypatch.setattr(benchmark.os, "environ", benchmark.os.environ.copy())
    monkeypatch.setenv("K8S_PROXY_PORT", "17443")
    monkeypatch.setattr(benchmark, "LAUNCHER", AgentLauncher())
    monkeypatch.setattr(benchmark, "init_logger", lambda: None)
    monkeypatch.setattr(benchmark, "set_profile", lambda profile: None)
    monkeypatch.setattr(benchmark, "_configure_model_environment", lambda args: ("unused", "unused"))
    monkeypatch.setattr(benchmark, "run_judge_preflight_check", lambda: None)
    monkeypatch.setattr(benchmark, "get_container_host_bind_address", lambda: "172.17.0.1")
    monkeypatch.setattr(ContainerRunner, "ensure_image_exists", lambda self: None)

    def preflight(agent, *, container_runner, install_script):
        assert container_runner.config.k8s_proxy_port == 17443
        raise StopBeforeDeployment

    monkeypatch.setattr(benchmark, "run_preflight_check", preflight)
    args = SimpleNamespace(
        agent="debug",
        internet_access="filtered",
        use_external_harness=False,
        profile="full",
        noise=False,
        force_build=False,
        stages=None,
    )
    with pytest.raises(StopBeforeDeployment):
        benchmark.main(args)
