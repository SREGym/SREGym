"""Single-turn, non-streaming Chat Completions bridge for the supported agent CLIs."""

import argparse
import contextlib
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "auto"
CLI_TIMEOUT_SECONDS = 300
# Effort levels the CLIs accept. Claude Code: `--effort`; Codex: `model_reasoning_effort`.
CLAUDECODE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def _flatten_messages(messages: list[dict]) -> str:
    """Turn a chat-completions messages array into a single CLI prompt."""
    parts = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, list):
            # Some clients send content as a list of {"type": "text", "text": ...} parts.
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


def _execute(command: list[str], *, cwd: Path, env: dict[str, str], prompt: str | None = None) -> str:
    # A timeout must stop the CLI's children as well as the CLI process itself.
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(prompt, timeout=CLI_TIMEOUT_SECONDS)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(f"{command[0]} exited {process.returncode}: {(stderr or stdout).strip()[:2000]}")
    return stdout


def _result_text(output: str) -> str:
    data = json.loads(output)
    if not isinstance(data, dict) or data.get("is_error"):
        raise RuntimeError(f"CLI reported an error: {output[:2000]}")
    result = data.get("result")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("CLI response had no result text")
    return result


def _run_cursor(prompt: str, model: str, cwd: Path, env: dict[str, str], effort: str | None = None) -> str:
    config = cwd / ".cursor"
    config.mkdir()
    permissions = {
        "allow": [],
        "deny": [
            "Shell(*)",
            "Read(**)",
            "Read(/**)",
            "Write(**)",
            "Write(/**)",
            "WebFetch(*)",
            "Mcp(*:*)",
        ],
    }
    (config / "cli.json").write_text(json.dumps({"permissions": permissions}))
    # Keep cached-login compatibility for users of the original manual bridge.
    if env.get("CURSOR_API_KEY"):
        env["CURSOR_CONFIG_DIR"] = str(config)
        (config / "cli-config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "editor": {"vimMode": False},
                    "permissions": permissions,
                }
            )
        )
    return _result_text(
        _execute(
            [
                "agent",
                "--print",
                prompt,
                "--model",
                model,
                "--output-format",
                "json",
                "--trust",
            ],
            cwd=cwd,
            env=env,
        )
    )


def _run_codex(prompt: str, model: str, cwd: Path, env: dict[str, str], effort: str | None = None) -> str:
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
        env.pop(key, None)
    response = cwd / "response.txt"
    effort_args = ["-c", f"model_reasoning_effort={effort}"] if effort in CODEX_EFFORTS else []
    output = _execute(
        [
            "codex",
            "exec",
            "--model",
            model,
            *effort_args,
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "-c",
            'web_search="disabled"',
            "-c",
            'cli_auth_credentials_store="file"',
            "-c",
            "features={shell_tool=false, unified_exec=false, apps=false, multi_agent=false, "
            "plugins=false, view_image=false, image_generation=false, browser_use=false, computer_use=false, "
            "skill_search=false, workspace_dependencies=false}",
            "--json",
            "--output-last-message",
            str(response),
            "-",
        ],
        cwd=cwd,
        env=env,
        prompt=prompt,
    )
    for line in output.splitlines():
        event = json.loads(line)
        if event.get("type") == "turn.failed":
            raise RuntimeError(f"Codex reported an error: {line[:2000]}")
    return response.read_text()


def _run_claudecode(prompt: str, model: str, cwd: Path, env: dict[str, str], effort: str | None = None) -> str:
    env.pop("ANTHROPIC_API_KEY", None)
    env["CLAUDE_CONFIG_DIR"] = str(cwd / ".claude")
    # Settings files are disabled below, so the effort has to travel on the command line.
    effort_args = ["--effort", effort] if effort in CLAUDECODE_EFFORTS else []
    # --bare disables subscription authentication, so use an empty configuration
    # and explicit tool controls with ordinary print mode instead.
    return _result_text(
        _execute(
            [
                "claude",
                "-p",
                "--model",
                model,
                *effort_args,
                "--output-format",
                "json",
                "--tools",
                "",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--no-session-persistence",
            ],
            cwd=cwd,
            env=env,
            prompt=prompt,
        )
    )


def _run_copilot(prompt: str, model: str, cwd: Path, env: dict[str, str], effort: str | None = None) -> str:
    for key in ("COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_API_KEY", "COPILOT_PROVIDER_TYPE", "COPILOT_ALLOW_ALL"):
        env.pop(key, None)
    env["COPILOT_HOME"] = str(cwd / ".copilot")
    return _execute(
        [
            "copilot",
            "--silent",
            "--model",
            model,
            "--output-format",
            "text",
            "--no-ask-user",
            "--no-custom-instructions",
            "--disable-builtin-mcps",
            "--deny-tool=read,write,shell,url,memory",
            "--excluded-tools=list_agents,read_agent,task,write_agent,skill",
        ],
        cwd=cwd,
        env=env,
        prompt=prompt,
    )


CLI_RUNNERS = {"cursor": _run_cursor, "codex": _run_codex, "claudecode": _run_claudecode, "copilot": _run_copilot}


def _run_agent(prompt: str, model: str, backend: str = "cursor", effort: str | None = None) -> str:
    with tempfile.TemporaryDirectory(prefix="sregym-judge-") as directory:
        result = CLI_RUNNERS[backend](prompt, model, Path(directory), os.environ.copy(), effort=effort)
        if not result.strip():
            raise RuntimeError(f"{backend} returned an empty judgment")
        return result


def make_handler(default_model: str, backend: str = "cursor", default_effort: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - matches base signature
            logger.info("%s - %s", self.address_string(), format % args)

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            if self.path in ("/health", "/health/liveliness"):
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"error": {"message": "not found"}})

        def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send_json(404, {"error": {"message": "not found"}})
                return

            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                request = json.loads(raw_body or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": {"message": "invalid JSON body"}})
                return

            messages = request.get("messages", [])
            model = request.get("model") or default_model
            # LiteLLM model strings are typically "openai/<name>"; the CLI
            # only wants the bare model id.
            if "/" in model:
                model = model.rsplit("/", 1)[-1]

            prompt = _flatten_messages(messages)
            # Chat Completions carries the OpenAI-style field; the bridge's own default applies otherwise.
            effort = request.get("reasoning_effort") or default_effort
            if effort == "none":
                effort = None

            try:
                text = _run_agent(prompt, model, backend, effort)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
                logger.error("%s judge bridge request failed: %s", backend, e)
                self._send_json(502, {"error": {"message": str(e), "code": f"{backend}_cli_error"}})
                return

            response = {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            }
            self._send_json(200, response)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=tuple(CLI_RUNNERS), default="cursor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Fallback model if the request omits one")
    parser.add_argument(
        "--effort", default=None, help="Effort level passed to CLIs that accept one (Claude Code, Codex)"
    )
    args = parser.parse_args()

    handler = make_handler(args.model, args.backend, args.effort)
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        logger.info("%s judge bridge listening on http://%s:%s", args.backend, args.host, args.port)
        server.serve_forever()


if __name__ == "__main__":
    main()
