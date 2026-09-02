import pytest

from clients.codex.codex_agent import CodexAgent, custom_provider_args


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
