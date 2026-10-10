"""File-based Claude authentication must not enter published run artifacts."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from clients.claudecode.claudecode_agent import ClaudeCodeAgent


@pytest.fixture
def agent(tmp_path):
    return ClaudeCodeAgent(logs_dir=tmp_path / "logs", model_name="claude-opus-5")


def write_credentials(path, token="oauth-access-token"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    return path


def test_a_subscription_credentials_file_counts_as_authentication(agent, tmp_path, monkeypatch):
    path = write_credentials(tmp_path / "creds" / ".credentials.json")
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS_FILE", str(path))

    assert agent._subscription_credentials() == path


def test_no_credentials_file_means_no_subscription_auth(agent, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS_FILE", str(tmp_path / "absent.json"))

    assert agent._subscription_credentials() is None


@pytest.mark.parametrize("content", ['{"other": true}', "not json", '{"claudeAiOauth": {}}'])
def test_a_file_without_an_access_token_is_not_subscription_auth(agent, tmp_path, monkeypatch, content):
    path = tmp_path / ".credentials.json"
    path.write_text(content)
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS_FILE", str(path))

    assert agent._subscription_credentials() is None


def test_a_symlinked_credentials_file_is_refused(agent, tmp_path, monkeypatch):
    real = write_credentials(tmp_path / "real" / ".credentials.json")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS_FILE", str(link))

    assert agent._subscription_credentials() is None


def test_credentials_are_installed_where_the_cli_will_look(agent, tmp_path, monkeypatch):
    """The agent overrides CLAUDE_CONFIG_DIR, so the file must be copied there.

    Without this the CLI authenticates against an empty config directory and
    reports no authentication even though the host is logged in.
    """
    source = write_credentials(tmp_path / "creds" / ".credentials.json")
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS_FILE", str(source))

    config = tmp_path / "private-config"
    config.mkdir(mode=0o700)
    agent._install_subscription_credentials(source, config)

    installed = config / ".credentials.json"
    assert json.loads(installed.read_text())["claudeAiOauth"]["accessToken"] == "oauth-access-token"
    # A copy, so a refresh inside the container cannot rewrite the read-only mount.
    assert not installed.is_symlink()
    assert installed.stat().st_mode & 0o777 == 0o600
    assert source.read_text() == installed.read_text()
    assert not (agent.sessions_dir / ".credentials.json").exists()


def test_an_explicit_env_token_still_takes_precedence(agent, tmp_path, monkeypatch):
    """An operator naming a token in the environment meant that token."""
    write_credentials(tmp_path / "creds" / ".credentials.json")
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS_FILE", str(tmp_path / "creds" / ".credentials.json"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "explicit")

    # The file is still discoverable, but the env token is what should be used,
    # and no credentials file should be planted in the sessions directory.
    assert agent._subscription_credentials() is not None
    assert not (agent.sessions_dir / ".credentials.json").exists()


def test_the_default_location_is_the_harness_mount_point():
    """The harness mounts the subscription file at this exact path."""
    from clients.claudecode.claudecode_agent import CREDENTIALS_FILE

    assert str(CREDENTIALS_FILE) == "/root/.claude/.credentials.json"
    assert os.path.isabs(CREDENTIALS_FILE)


@pytest.mark.parametrize("authentication", ["file", "api", "oauth"])
@pytest.mark.parametrize("outcome", ["success", "nonzero", "start_error"])
def test_cli_config_stays_private_and_only_sessions_are_saved(agent, tmp_path, monkeypatch, authentication, outcome):
    source = write_credentials(tmp_path / "creds" / ".credentials.json")
    original = source.read_text()
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS_FILE", str(source))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    if authentication == "api":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key-for-test")
    elif authentication == "oauth":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "judge-key-for-test")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token-for-test")

    configs = []

    def start(command, **kwargs):
        config = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        configs.append(config)
        assert not config.is_relative_to(agent.logs_dir)
        assert config.stat().st_mode & 0o777 == 0o700
        installed = config / ".credentials.json"
        assert installed.exists() is (authentication == "file")
        if installed.exists():
            assert installed.stat().st_mode & 0o777 == 0o600
            installed.write_text("refreshed-test-credentials")
        if authentication == "oauth":
            assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        if outcome == "start_error":
            raise OSError("CLI could not start")
        (config / "projects" / "-app" / "session.jsonl").write_text('{"type":"assistant"}\n')
        (config / "debug.log").write_text("private CLI state")
        return SimpleNamespace(stdout=iter([]), wait=lambda: None, returncode=int(outcome == "nonzero"))

    monkeypatch.setattr("clients.claudecode.claudecode_agent.subprocess.Popen", start)
    if outcome == "start_error":
        with pytest.raises(OSError, match="CLI could not start"):
            agent.run("test instruction")
    else:
        assert agent.run("test instruction") == int(outcome == "nonzero")
        assert (agent.sessions_dir / "projects" / "-app" / "session.jsonl").is_file()
    assert source.read_text() == original
    assert configs and not configs[0].exists()
    assert not list(agent.logs_dir.rglob(".credentials.json"))
    assert not list(agent.logs_dir.rglob("debug.log"))
