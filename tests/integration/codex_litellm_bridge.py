#!/usr/bin/env python3
"""Exercise the real Codex -> LiteLLM -> Chat Completions bridge locally.

The mock upstream enforces the subset accepted by the Z.ai Coding Plan endpoint:
requests must target ``/v4/chat/completions`` and every tool must be a function.
No API key or model credits are required.

Run from the repository root:

    uv run python tests/integration/codex_litellm_bridge.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MODEL = "glm-5.3-flash"
MASTER_KEY = "sk-local-bridge-test"


class UpstreamState:
    """Requests observed by the mock Chat Completions endpoint."""

    def __init__(self) -> None:
        self.tool_batches: list[list[dict[str, Any]]] = []


def _handler_for(state: UpstreamState):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("content-length", "0"))
            request = json.loads(self.rfile.read(length))

            if self.path != "/v4/chat/completions":
                self._send_json(404, {"error": {"message": f"unexpected path: {self.path}"}})
                return

            tools = request.get("tools", [])
            state.tool_batches.append(tools)
            for index, tool in enumerate(tools):
                if tool.get("type") != "function":
                    self._send_json(
                        400,
                        {
                            "error": {
                                "code": "1214",
                                "message": f"tools[{index}].type:type is illegal",
                            }
                        },
                    )
                    return

            if request.get("stream"):
                chunks = [
                    {
                        "id": "chatcmpl-bridge-test",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "OK"},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-bridge-test",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": MODEL,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                ]
                encoded = ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return

            self._send_json(
                200,
                {
                    "id": "chatcmpl-bridge-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_proxy(port: int, process: subprocess.Popen[str], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health/liveliness"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"LiteLLM exited before becoming ready (status {process.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310 - loopback test server
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise TimeoutError(f"LiteLLM did not become ready within {timeout:.0f}s")


def _run_checked(label: str, command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode:
        output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        raise RuntimeError(f"{label} failed with status {result.returncode}:\n{output}")
    print(f"PASS: {label}")


def _tool_name(tool: dict[str, Any]) -> str | None:
    function = tool.get("function")
    return function.get("name") if isinstance(function, dict) else None


def run(litellm_version: str) -> None:
    for executable in ("codex", "uvx"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"{executable} is required but was not found in PATH")

    state = UpstreamState()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(state))
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    upstream_port = int(upstream.server_address[1])
    proxy_port = _unused_port()

    with tempfile.TemporaryDirectory(prefix="sregym-codex-bridge-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        config_path = temp_dir / "litellm.yaml"
        proxy_log_path = temp_dir / "litellm.log"
        config_path.write_text(
            f"""model_list:
  - model_name: {MODEL}
    litellm_params:
      model: openai/chat_completions/{MODEL}
      api_base: http://127.0.0.1:{upstream_port}/v4
      api_key: fake-upstream-key
general_settings:
  master_key: {MASTER_KEY}
litellm_settings:
  drop_params: true
  modify_params: true
"""
        )

        with proxy_log_path.open("w") as proxy_log:
            proxy = subprocess.Popen(
                [
                    "uvx",
                    "--from",
                    f"litellm[proxy]=={litellm_version}",
                    "litellm",
                    "--config",
                    str(config_path),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(proxy_port),
                ],
                cwd=ROOT,
                stdout=proxy_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                _wait_for_proxy(proxy_port, proxy)
                codex_home = temp_dir / "codex-home"
                codex_home.mkdir()
                env = dict(os.environ)
                env.update(
                    {
                        "AGENT_API_BASE": f"http://127.0.0.1:{proxy_port}/v1",
                        "AGENT_API_KEY": MASTER_KEY,
                        "AGENT_MODEL_ID": MODEL,
                        "CODEX_HOME": str(codex_home),
                        "SREGYM_BRIDGE_LOGS": str(temp_dir / "agent-logs"),
                    }
                )
                env.pop("OPENAI_API_KEY", None)

                _run_checked(
                    "Codex driver preflight",
                    [
                        sys.executable,
                        "-c",
                        "from clients.codex.driver import run_preflight; run_preflight()",
                    ],
                    env,
                )
                _run_checked(
                    "Codex agent command",
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os; from pathlib import Path; "
                            "from clients.codex.codex_agent import CodexAgent; "
                            "agent = CodexAgent(Path(os.environ['SREGYM_BRIDGE_LOGS']), "
                            "os.environ['AGENT_MODEL_ID']); "
                            "raise SystemExit(agent.run('say ok'))"
                        ),
                    ],
                    env,
                )
            except Exception:
                proxy_log.flush()
                print("\nLiteLLM log:\n" + proxy_log_path.read_text(), file=sys.stderr)
                raise
            finally:
                proxy.terminate()
                try:
                    proxy.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait(timeout=5)
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=5)

    if len(state.tool_batches) < 2:
        raise RuntimeError(f"expected at least two upstream requests, observed {len(state.tool_batches)}")

    required_tools = {"exec_command", "write_stdin"}
    for index, tools in enumerate(state.tool_batches, start=1):
        names = {_tool_name(tool) for tool in tools}
        missing = required_tools - names
        if missing:
            raise RuntimeError(f"request {index} did not include required tools: {sorted(missing)}")
        if any(tool.get("type") != "function" for tool in tools):
            raise RuntimeError(f"request {index} included a non-function tool: {tools}")
        print(f"PASS: upstream request {index} used {len(tools)} function tools")

    version = subprocess.run(["codex", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    print(f"Bridge compatible: {version}, LiteLLM {litellm_version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--litellm-version", default="1.91.0", help="LiteLLM proxy version to exercise")
    args = parser.parse_args()
    run(args.litellm_version)


if __name__ == "__main__":
    main()
