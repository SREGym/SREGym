"""Subscription auth for Claude Code is a credentials file, not an env var.

Running the first screen found both halves of this broken: the client rejected
the run outright for having "no authentication", and even past that check it
points `CLAUDE_CONFIG_DIR` at its own sessions directory, so the CLI would not
have read the mounted `~/.claude` anyway.
"""

import json
import os

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

    agent._install_subscription_credentials(source)

    installed = agent.sessions_dir / ".credentials.json"
    assert json.loads(installed.read_text())["claudeAiOauth"]["accessToken"] == "oauth-access-token"
    # A copy, so a refresh inside the container cannot rewrite the read-only mount.
    assert not installed.is_symlink()
    assert installed.stat().st_mode & 0o777 == 0o600
    assert source.read_text() == installed.read_text()


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
