"""Claude Code and Codex run on AWS Bedrock with a bedrock/<inference-profile> model id."""

import base64
import contextlib
import os
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

PROFILE = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


@pytest.fixture
def aws(monkeypatch, tmp_path):
    # Fake static credentials; nothing here talks to AWS.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "AGENT_API_BASE", "AGENT_API_KEY"):
        monkeypatch.setenv(name, "x")  # recorded so the test restores the original state
        monkeypatch.delenv(name)


def _run_preflight(monkeypatch, module, model):
    monkeypatch.setenv("AGENT_MODEL_ID", model)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as result:
        module.run_preflight()
    assert result.value.code == 0
    return calls[0]


def _judge_credentials(monkeypatch):
    # The host's Anthropic credentials belong to the judge and must not reach a Bedrock agent.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "judge-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "judge-oauth")


def test_claude_code_preflight_uses_bedrock(monkeypatch, aws):
    from clients.claudecode import driver

    _judge_credentials(monkeypatch)
    command, env = _run_preflight(monkeypatch, driver, f"bedrock/{PROFILE}")

    assert command[command.index("--model") + 1] == PROFILE
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@pytest.mark.parametrize("prefix", ["bedrock", "Bedrock"])
def test_claude_code_run_uses_bedrock_without_anthropic_keys(monkeypatch, aws, tmp_path, prefix):
    from clients.claudecode import claudecode_agent
    from clients.claudecode.claudecode_agent import ClaudeCodeAgent

    _judge_credentials(monkeypatch)
    seen = {}

    class Stop(Exception):
        pass

    def fake_popen(command, **kwargs):
        seen.update(command=command, env=kwargs["env"])
        raise Stop

    monkeypatch.setattr(claudecode_agent.subprocess, "Popen", fake_popen)
    agent = ClaudeCodeAgent(logs_dir=tmp_path, model_name=f"{prefix}/{PROFILE}")

    with contextlib.suppress(Stop):
        agent.run("inspect the cluster")

    assert seen["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert seen["env"]["ANTHROPIC_MODEL"] == PROFILE
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]


def test_codex_preflight_uses_bedrock_responses_endpoint(monkeypatch, aws, tmp_path):
    from clients.codex import driver

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)  # an ambient profile would be looked up in the fake config

    command, env = _run_preflight(monkeypatch, driver, f"bedrock/{PROFILE}")

    assert (
        'model_providers.sregym_custom.base_url="https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"' in command
    )
    assert 'web_search="disabled"' in command
    token = env["AGENT_API_KEY"].removeprefix("bedrock-api-key-")
    url = urlsplit("https://" + base64.b64decode(token).decode())
    query = parse_qs(url.query)
    assert url.hostname == "bedrock.amazonaws.com"
    assert query["Action"] == ["CallWithBearerToken"]
    assert query["X-Amz-Expires"] == ["43200"]
    assert query["Version"] == ["1"]


def test_non_bedrock_codex_model_is_untouched(monkeypatch, aws):
    from clients.codex.codex_agent import bedrock_runtime_args, use_bedrock_provider

    use_bedrock_provider("gpt-5")

    assert "AGENT_API_BASE" not in os.environ
    assert bedrock_runtime_args("gpt-5") == []


def test_codex_run_uses_bedrock_responses_endpoint(monkeypatch, aws, tmp_path):
    from clients.codex import codex_agent
    from clients.codex.codex_agent import CodexAgent

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    seen = {}

    class Stop(Exception):
        pass

    def fake_popen(command, **kwargs):
        seen.update(command=command, env=kwargs["env"])
        raise Stop

    monkeypatch.setattr(codex_agent.subprocess, "Popen", fake_popen)
    agent = CodexAgent(logs_dir=tmp_path, model_name=f"bedrock/{PROFILE}")

    with contextlib.suppress(Stop):
        agent.run("inspect the cluster")

    assert seen["env"]["AGENT_API_BASE"] == "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"
    assert seen["env"]["AGENT_API_KEY"].startswith("bedrock-api-key-")
    assert 'web_search="disabled"' in seen["command"]


@pytest.mark.parametrize("model", [f"bedrock/{PROFILE}", f"Bedrock/{PROFILE}"])
def test_runner_keeps_judge_credentials_out_of_a_bedrock_agent_container(monkeypatch, aws, tmp_path, model):
    from sregym.service.container_runner import ContainerConfig, ContainerRunner

    _judge_credentials(monkeypatch)

    env = ContainerRunner(ContainerConfig(logs_path=tmp_path))._build_env_vars({"AGENT_MODEL_ID": model})

    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_runner_forwards_anthropic_credentials_to_a_non_bedrock_agent(monkeypatch, aws, tmp_path):
    from sregym.service.container_runner import ContainerConfig, ContainerRunner

    _judge_credentials(monkeypatch)

    env = ContainerRunner(ContainerConfig(logs_path=tmp_path))._build_env_vars({"AGENT_MODEL_ID": "claude-opus-5-5"})

    assert env["ANTHROPIC_API_KEY"] == "judge-key"


def test_runner_passes_resolved_aws_credentials_to_a_bedrock_agent(monkeypatch, aws, tmp_path):
    # A filtered agent container cannot reach the instance metadata service, so the runner
    # resolves the host's AWS chain (here a profile) and passes plain keys in.
    from sregym.service.container_runner import ContainerConfig, ContainerRunner

    creds = tmp_path / "credentials"
    creds.write_text("[bench]\naws_access_key_id = AKIDPROFILE\naws_secret_access_key = profile-secret\n")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("AWS_PROFILE", "bench")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    env = ContainerRunner(ContainerConfig(logs_path=tmp_path))._build_env_vars({"AGENT_MODEL_ID": f"bedrock/{PROFILE}"})

    assert env["AWS_ACCESS_KEY_ID"] == "AKIDPROFILE"
    assert env["AWS_SECRET_ACCESS_KEY"] == "profile-secret"
    assert env["AWS_REGION"] == "us-west-2"


def test_runner_does_not_resolve_aws_credentials_for_other_models(monkeypatch, aws, tmp_path):
    from sregym.service import container_runner
    from sregym.service.container_runner import ContainerConfig, ContainerRunner

    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    monkeypatch.setattr(container_runner, "_host_aws_credentials", lambda: pytest.fail("resolved AWS credentials"))

    env = ContainerRunner(ContainerConfig(logs_path=tmp_path))._build_env_vars({"AGENT_MODEL_ID": "claude-opus-5-5"})

    assert "AWS_ACCESS_KEY_ID" not in env
