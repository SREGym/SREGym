import json

from clients.claudecode.claudecode_agent import ClaudeCodeAgent
from sregym.service.container_runner import ContainerConfig, ContainerRunner

URL = "https://chiron.example/mcp"
TOKEN = "test-token-value"


def test_without_url_no_chiron_tools_or_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRON_MCP_URL", raising=False)
    agent = ClaudeCodeAgent(logs_dir=tmp_path, model_name="claude-sonnet-5")

    assert not [tool for tool in ClaudeCodeAgent.allowed_tools() if tool.startswith("mcp__chiron__")]
    assert agent._write_chiron_mcp_config() is None
    assert not (tmp_path / "chiron-mcp.json").exists()


def test_url_adds_read_only_tools_and_a_config_without_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIRON_MCP_URL", URL)
    monkeypatch.setenv("CHIRON_MCP_TOKEN", TOKEN)
    agent = ClaudeCodeAgent(logs_dir=tmp_path, model_name="claude-sonnet-5")

    assert set(ClaudeCodeAgent.CHIRON_MCP_TOOLS) <= set(ClaudeCodeAgent.allowed_tools())
    text = agent._write_chiron_mcp_config().read_text()
    server = json.loads(text)["mcpServers"]["chiron"]
    assert server["url"] == URL
    assert server["headers"]["Authorization"] == "Bearer ${CHIRON_MCP_TOKEN}"
    assert TOKEN not in text


def test_runner_forwards_chiron_settings_only_with_host_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIRON_MCP_URL", URL)
    monkeypatch.setenv("CHIRON_MCP_TOKEN", TOKEN)

    env = ContainerRunner(ContainerConfig(logs_path=tmp_path))._build_env_vars()
    assert env["CHIRON_MCP_URL"] == URL
    assert env["CHIRON_MCP_TOKEN"] == TOKEN

    env = ContainerRunner(ContainerConfig(logs_path=tmp_path, forward_host_credentials=False))._build_env_vars()
    assert "CHIRON_MCP_URL" not in env
    assert "CHIRON_MCP_TOKEN" not in env
