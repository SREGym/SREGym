"""Tests for the Claude Code + Radar client: the agent's CLI wiring, task instruction and cluster access."""

import io
import json

import pytest

from clients.claudecode.claudecode_agent import ClaudeCodeAgent
from clients.claudecode_radar import claudecode_agent as agent
from clients.claudecode_radar.claudecode_agent import ROUTING_PATH, RadarClaudeCodeAgent


def test_radar_tools_extend_the_stock_allowlist(monkeypatch):
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "filtered")

    stock = ClaudeCodeAgent.allowed_tools()
    radar = RadarClaudeCodeAgent.allowed_tools()

    assert radar[: len(stock)] == stock
    assert {"mcp__radar", "ToolSearch"} <= set(radar)
    assert "WebFetch" not in radar


def test_mcp_config_points_at_the_configured_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("RADAR_MCP_URL", "http://radar.test:9999/mcp")
    radar_agent = RadarClaudeCodeAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-5-5")

    config = json.loads(radar_agent._write_mcp_config().read_text())

    assert config == {"mcpServers": {"radar": {"type": "http", "url": "http://radar.test:9999/mcp"}}}


def test_routing_names_the_tools_and_nothing_sregym_specific():
    text = ROUTING_PATH.read_text()
    for tool in ("exec_pod", "delete_resource", "wait_for"):
        assert tool in text
    for word in ("SREGym", "sregym", "hotel", "astronomy", "social-network", "oracle", "submit"):
        assert word not in text


def test_instruction_rewrites_only_the_tool_line():
    import clients.claudecode.driver as upstream
    from clients.claudecode_radar import driver

    app = {"app_name": "demo", "namespaces": ["ns1"], "descriptions": "d"}
    stock = driver._upstream_build_instruction(app)
    assert driver.KUBECTL_TOOL_LINE in stock

    rewritten = driver.build_instruction(app)
    assert "kubectl" not in rewritten
    assert driver.RADAR_TOOL_LINE + "ns1" in rewritten
    assert rewritten.replace(driver.RADAR_TOOL_LINE, driver.KUBECTL_TOOL_LINE) == stock
    # The client reuses the stock driver by replacing its agent class and instruction builder when
    # this module is imported; the driver runs as its own process (python -m clients.claudecode_radar.driver).
    assert upstream.build_instruction is driver.build_instruction


def test_kubectl_is_removed_and_a_kubeconfig_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "kubectl").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(agent, "AGENT_KUBECONFIG", tmp_path / "absent")
    agent.remove_cluster_access_from_container()
    assert not (bindir / "kubectl").exists()

    # a kubeconfig still present means the registration lacks cluster_access: false
    present = tmp_path / "config"
    present.write_text("x")
    monkeypatch.setattr(agent, "AGENT_KUBECONFIG", present)
    with pytest.raises(RuntimeError):
        agent.remove_cluster_access_from_container()

    monkeypatch.setattr(agent, "AGENT_KUBECONFIG", tmp_path / "absent")
    monkeypatch.setenv("KUBECONFIG", "/somewhere")
    with pytest.raises(RuntimeError):
        agent.remove_cluster_access_from_container()


def test_cli_command(monkeypatch, tmp_path):
    captured = {}

    class FakeProcess:
        stdout = io.StringIO("")
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent, "remove_cluster_access_from_container", lambda: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "medium")
    radar_agent = RadarClaudeCodeAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-5-5")

    assert radar_agent.run("task") == 0

    command = captured["command"]
    assert command[command.index("--mcp-config") + 1].endswith("radar-mcp-config.json")
    assert "--strict-mcp-config" in command
    assert command[command.index("--append-system-prompt") + 1] == ROUTING_PATH.read_text()
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--effort") + 1] == "medium"
    assert "--settings" not in command  # no hooks: kubectl is absent, not blocked
    assert "mcp__radar" in command[command.index("--allowedTools") + 1 :]


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ('{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"radar","version":"1.0.0"}}}', True),
        ('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"radar","version":"1"}}}', True),
        ('{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"initialization failed"}}', False),
    ],
)
def test_preflight_requires_an_initialize_result(monkeypatch, body, ok):
    from clients.claudecode_radar import driver

    class Response:
        status_code = 200
        text = body

    monkeypatch.setattr(driver.requests, "post", lambda *a, **kw: Response())
    if ok:
        driver._check_radar_mcp()
    else:
        with pytest.raises(SystemExit):
            driver._check_radar_mcp()
