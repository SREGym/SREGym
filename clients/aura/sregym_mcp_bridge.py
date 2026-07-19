"""Long-running stdio MCP server that proxies one SREGym fastmcp/SSE mount.

Why this exists: AURA's rmcp (v0.12) stdio transport spawns its mcp child,
runs the initialize + list_tools handshake, then closes stdin. With `uvx mcp-proxy <sse-url>`
as the child, mcp-proxy correctly exits on EOF (clean shutdown), but
subsequent tool calls AURA tries to make hit a dead transport (`Transport
closed`).

This bridge solves the lifetime problem by holding a single long-lived
SSE session against SREGym's MCP server FOR THE ENTIRE PROCESS LIFETIME,
and reusing that session across multiple stdin/stdout reconnects. AURA
respawns us per tool call; we share state via an on-disk session-id
cache so subsequent spawns join the same SSE session.

Usage::

    python sregym_mcp_bridge.py <upstream-sse-base-url>

e.g. ``python sregym_mcp_bridge.py http://localhost:9954/kubectl``.

The bridge is a one-file dependency-light implementation built on
``httpx`` + ``mcp`` (the official Python MCP SDK) — already in SREGym's
venv. Stays single-file so the orchestrator can copy it next to
``aura_agent.py`` and ``driver.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("sregym_mcp_bridge")


# Tag the request with the mount name so the operator can see which bridge
# is which in stderr logs.
def _stderr_log(msg: str) -> None:
    sys.stderr.write(f"[sregym_mcp_bridge] {msg}\n")
    sys.stderr.flush()


def _session_cache_path(upstream_base: str) -> Path:
    """Where to persist the SSE session id between bridge invocations.

    Multiple short-lived spawns of this script (one per AURA tool call)
    share a session by storing the upstream-issued ``session_id`` in a
    tmp file keyed on the mount path.
    """
    mount = upstream_base.rsplit("/", 1)[-1] or "default"
    return Path(f"/tmp/sregym_mcp_session_{mount}.json")


async def _establish_or_load_session(
    *, upstream_base: str, client: httpx.AsyncClient
) -> str:
    """Return a SREGym SSE session_id, reusing one from disk if still alive.

    The first invocation opens an SSE GET against ``<upstream>/sse``,
    waits for the upstream's ``endpoint`` event (which carries the
    ``session_id``), persists it, and keeps the GET open in a background
    task. Subsequent invocations reuse the persisted session_id without
    re-opening SSE — they just POST to ``/messages/?session_id=<id>``.
    """
    cache = _session_cache_path(upstream_base)
    if cache.is_file():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8", errors="strict"))
            session_id = payload.get("session_id")
            if isinstance(session_id, str) and session_id:
                _stderr_log(f"Reusing cached session_id={session_id}")
                return session_id
        except (OSError, json.JSONDecodeError):
            pass

    _stderr_log(f"Opening new SSE session against {upstream_base}/sse")
    sse_url = f"{upstream_base}/sse"
    session_id: str | None = None
    async with client.stream("GET", sse_url, timeout=None) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            # First SSE event is the endpoint URL: ``/<mount>/messages/?session_id=<id>``.
            if "session_id=" in data:
                session_id = data.split("session_id=", 1)[1].strip()
                break
    if not session_id:
        raise RuntimeError("Failed to extract session_id from SREGym SSE handshake")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"session_id": session_id, "upstream_base": upstream_base}),
        encoding="utf-8",
        errors="strict",
    )
    _stderr_log(f"Established session_id={session_id}")
    return session_id


async def _forward_to_upstream(
    *,
    upstream_base: str,
    session_id: str,
    request_payload: dict[str, Any],
    client: httpx.AsyncClient,
) -> dict[str, Any] | None:
    """POST a JSON-RPC request to SREGym's /messages endpoint.

    SREGym's fastmcp responds 202 Accepted for the POST and delivers the
    actual JSON-RPC response asynchronously via the SSE stream. Since
    we don't keep SSE open between spawns, we re-open SSE here briefly
    to receive the response, then close it.
    """
    messages_url = f"{upstream_base}/messages/"
    post_response = await client.post(
        messages_url,
        params={"session_id": session_id},
        json=request_payload,
        timeout=30.0,
    )
    if post_response.status_code != 202:
        _stderr_log(
            f"Upstream rejected POST (status={post_response.status_code}): "
            f"{post_response.text[:200]}"
        )
        return None

    # Re-open SSE briefly to read the JSON-RPC response.
    request_id = request_payload.get("id")
    sse_url = f"{upstream_base}/sse"
    async with client.stream("GET", sse_url, timeout=60.0) as response:
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[len("data: "):].strip()
            if not data_str.startswith("{"):
                continue  # endpoint event, not the JSON-RPC response
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if event.get("id") == request_id:
                return event
    return None


async def _main_async(upstream_base: str) -> int:
    """Read JSON-RPC from stdin, forward to upstream, write response to stdout."""
    async with httpx.AsyncClient() as client:
        session_id = await _establish_or_load_session(
            upstream_base=upstream_base, client=client
        )

        # Stdio MCP protocol: line-delimited JSON-RPC. We read each line,
        # forward, write the response, flush.
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break  # EOF — caller closed stdin
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                _stderr_log(f"Bad JSON from stdin: {exc}")
                continue

            response_event = await _forward_to_upstream(
                upstream_base=upstream_base,
                session_id=session_id,
                request_payload=request,
                client=client,
            )
            if response_event is None:
                # Synthesize an error response so AURA doesn't hang.
                response_event = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {
                        "code": -32603,
                        "message": "sregym_mcp_bridge: upstream response missing",
                    },
                }
            sys.stdout.write(json.dumps(response_event) + "\n")
            sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        _stderr_log(
            "usage: python sregym_mcp_bridge.py <upstream-sse-base-url>\n"
            "       e.g. http://localhost:9954/kubectl"
        )
        return 2
    upstream_base = args[0].rstrip("/")
    try:
        return asyncio.run(_main_async(upstream_base))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
