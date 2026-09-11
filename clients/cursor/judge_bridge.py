"""
Minimal OpenAI-Chat-Completions-compatible bridge in front of the Cursor CLI.

The problem-difficulty-evaluation workflow's LLM-as-a-judge talks to whatever
JUDGE_API_BASE/JUDGE_API_KEY point at via LiteLLM's OpenAI-compatible client
(see llm_backend/get_llm_backend.py). Cursor has no such HTTP inference API of
its own -- only the interactive/headless `agent` CLI -- so when the Cursor
fallback is in use for the agent, this bridge stands in for the judge too by
shelling out to `agent --print ... --output-format json` per request and
wrapping the result in a standard chat-completion response.

This is intentionally single-purpose: single-turn, non-streaming, no tool
use. It exists only so the existing judge code path needs no changes.

Usage:
    python -m clients.cursor.judge_bridge --port 4100 --model auto
"""

import argparse
import json
import logging
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("all.cursor.judge_bridge")

DEFAULT_MODEL = "auto"
CLI_TIMEOUT_SECONDS = 300


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


def _run_agent(prompt: str, model: str) -> str:
    """Run the Cursor CLI once, non-interactively, and return its text result."""
    command = [
        "agent",
        "--print",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--trust",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Cursor CLI exited {result.returncode}: {(result.stderr or result.stdout).strip()[:2000]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Cursor CLI did not return valid JSON: {result.stdout[:2000]}") from e

    if data.get("is_error"):
        raise RuntimeError(f"Cursor CLI reported an error: {data}")

    text = data.get("result")
    if not isinstance(text, str):
        raise RuntimeError(f"Cursor CLI response had no 'result' text: {data}")
    return text


def make_handler(default_model: str):
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

            try:
                text = _run_agent(prompt, model)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                logger.error(f"Cursor judge bridge request failed: {e}")
                self._send_json(502, {"error": {"message": str(e), "code": "cursor_cli_error"}})
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
                # Cursor's CLI does not report token counts; zeros keep the
                # response schema-valid without fabricating usage data.
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            self._send_json(200, response)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    parser = argparse.ArgumentParser(description="OpenAI-compatible bridge in front of the Cursor CLI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Fallback model if the request omits one")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.model))
    logger.info(f"Cursor judge bridge listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
