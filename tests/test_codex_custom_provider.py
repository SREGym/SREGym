import subprocess

import pytest

from clients.codex.codex_agent import CodexAgent, custom_provider_args
from sregym.service.container_runner import ContainerConfig, ContainerRunner
from sregym.service.internet_policy import InternetPolicy


def test_custom_provider_is_disabled_without_agent_api_base(monkeypatch):
    monkeypatch.delenv("AGENT_API_BASE", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)

    assert custom_provider_args() == []


def test_custom_provider_requires_api_key(monkeypatch):
    monkeypatch.setenv("AGENT_API_BASE", "https://proxy.example.test/v1")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="AGENT_API_KEY is required"):
        custom_provider_args()


def test_custom_provider_uses_responses_wire_api_without_exposing_key(monkeypatch, tmp_path):
    api_key = "secret-provider-key"
    monkeypatch.setenv("AGENT_API_BASE", "https://proxy.example.test/v1")
    monkeypatch.setenv("AGENT_API_KEY", api_key)
    agent = CodexAgent(logs_dir=tmp_path, model_name="glm-4.7")

    command = agent._build_command("inspect the cluster")
    joined = " ".join(command)

    assert 'model_provider="sregym_custom"' in command
    assert 'model_providers.sregym_custom.base_url="https://proxy.example.test/v1"' in command
    assert 'model_providers.sregym_custom.env_key="AGENT_API_KEY"' in command
    assert 'model_providers.sregym_custom.wire_api="responses"' in command
    assert api_key not in joined


def test_custom_provider_preflight_creates_codex_home(monkeypatch, tmp_path):
    from clients.codex.driver import run_preflight

    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("AGENT_API_BASE", "https://proxy.example.test/v1")
    monkeypatch.setenv("AGENT_API_KEY", "provider-key")
    monkeypatch.setenv("AGENT_MODEL_ID", "glm-5.3-flash")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def check_home(command, **kwargs):
        assert home.is_dir()
        assert kwargs["env"]["CODEX_HOME"] == str(home)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", check_home)
    with pytest.raises(SystemExit) as result:
        run_preflight()
    assert result.value.code == 0


def test_custom_provider_uses_existing_home_with_pinned_agent_image():
    runner = ContainerRunner(
        ContainerConfig(
            env_vars={"AGENT_API_BASE": "https://proxy.example.test/v1"},
            internet_policy=InternetPolicy.from_mode("filtered", agent_name="codex"),
            forward_host_credentials=False,
        )
    )

    assert runner._build_env_vars()["CODEX_HOME"] == "/logs"
    runner.config.env_vars.clear()
    assert "CODEX_HOME" not in runner._build_env_vars()
