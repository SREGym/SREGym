import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_backend import judge_bridge


def _option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def test_run_agent_uses_fresh_cwds_and_rejects_empty_judgment(monkeypatch):
    seen = []

    def fake_runner(prompt: str, model: str, cwd: Path, env: dict[str, str]) -> str:
        assert cwd.is_dir()
        seen.append(cwd)
        return "judgment"

    monkeypatch.setitem(judge_bridge.CLI_RUNNERS, "cursor", fake_runner)
    assert judge_bridge._run_agent("prompt", "model", "cursor") == "judgment"
    assert judge_bridge._run_agent("prompt", "model", "cursor") == "judgment"
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(not cwd.exists() for cwd in seen)

    monkeypatch.setitem(judge_bridge.CLI_RUNNERS, "cursor", lambda *_args: " \n")
    with pytest.raises(RuntimeError, match="empty judgment"):
        judge_bridge._run_agent("prompt", "model", "cursor")


@pytest.mark.parametrize("terminal_event", ["turn.completed", "turn.failed"])
def test_codex_uses_last_message_and_terminal_outcome_after_reconnect(monkeypatch, tmp_path, terminal_event):
    api_overrides = dict.fromkeys(("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"), "unused")

    def fake_execute(command, *, cwd, env, prompt=None):
        assert command[0] == "codex" and _option(command, "--model") == "gpt-model"
        assert prompt == "prompt" and not api_overrides.keys() & env.keys()
        Path(_option(command, "--output-last-message")).write_text("judgment")
        return '{"type":"error","message":"Reconnecting... 1/5"}\n' + json.dumps({"type": terminal_event})

    monkeypatch.setattr(judge_bridge, "_execute", fake_execute)

    if terminal_event == "turn.failed":
        with pytest.raises(RuntimeError, match="Codex reported an error"):
            judge_bridge._run_codex("prompt", "gpt-model", tmp_path, api_overrides.copy())
    else:
        assert judge_bridge._run_codex("prompt", "gpt-model", tmp_path, api_overrides.copy()) == "judgment"


@pytest.mark.parametrize("backend,executable", [("claudecode", "claude"), ("copilot", "copilot"), ("cursor", "agent")])
def test_cli_runners_extract_results_and_scope_env(monkeypatch, tmp_path, backend, executable):
    copilot_overrides = {
        "COPILOT_PROVIDER_BASE_URL": "https://example.invalid",
        "COPILOT_PROVIDER_API_KEY": "provider-api",
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_ALLOW_ALL": "true",
    }
    env = {
        **copilot_overrides,
        "ANTHROPIC_API_KEY": "api",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        "COPILOT_GITHUB_TOKEN": "github-token",
        "CURSOR_API_KEY": "cursor-token",
    }

    def execute(command, *, cwd, env, prompt=None):
        assert command[0] == executable and _option(command, "--model") == "judge-model"
        assert (cwd, prompt) == (tmp_path, None if backend == "cursor" else "judge this")
        return "judgment" if backend == "copilot" else '{"is_error": false, "result": "judgment"}'

    monkeypatch.setattr(judge_bridge, "_execute", execute)
    assert getattr(judge_bridge, f"_run_{backend}")("judge this", "judge-model", tmp_path, env) == "judgment"
    if backend == "claudecode":
        assert "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
        assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude")
    elif backend == "copilot":
        assert not copilot_overrides.keys() & env.keys() and env["COPILOT_GITHUB_TOKEN"] == "github-token"
        assert env["COPILOT_HOME"] == str(tmp_path / ".copilot")
    else:
        assert env["CURSOR_CONFIG_DIR"] == str(tmp_path / ".cursor")
        config = json.loads((tmp_path / ".cursor" / "cli-config.json").read_text())
        assert config["permissions"]["allow"] == []
        assert config["permissions"]["deny"]


@pytest.mark.parametrize("runner_name", ["_run_cursor", "_run_claudecode"])
@pytest.mark.parametrize(
    "output,error",
    [
        ("not-json", ValueError),
        ("[]", RuntimeError),
        ('{"is_error": true, "result": "failed"}', RuntimeError),
        ('{"result": ""}', RuntimeError),
        ("{}", RuntimeError),
    ],
)
def test_json_cli_runners_reject_invalid_and_failed_results(monkeypatch, tmp_path, runner_name, output, error):
    monkeypatch.setattr(judge_bridge, "_execute", lambda *args, **kwargs: output)
    with pytest.raises(error):
        getattr(judge_bridge, runner_name)("prompt", "model", tmp_path, {})


def test_execute_raises_for_nonzero_subprocess(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"exited 7"):
        judge_bridge._execute(
            [sys.executable, "-c", "import sys; print('provider failed'); sys.exit(7)"],
            cwd=tmp_path,
            env={"PYTHONUNBUFFERED": "1"},
        )


def _proc_state(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return fields[2] if len(fields) > 2 else None


def test_execute_timeout_kills_child_process_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if not Path("/proc").is_dir():
        pytest.skip("process-group state inspection requires /proc")

    monkeypatch.setattr(judge_bridge, "CLI_TIMEOUT_SECONDS", 1.0)
    pid_file = tmp_path / "child.pid"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        judge_bridge._execute(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env={"PYTHONUNBUFFERED": "1"},
        )

    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2.0
    state = _proc_state(child_pid)
    while state not in (None, "Z") and time.monotonic() < deadline:
        time.sleep(0.02)
        state = _proc_state(child_pid)
    assert state in (None, "Z")
