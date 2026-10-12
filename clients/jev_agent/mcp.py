"""Thin synchronous client for the SREGym MCP servers (prometheus, jaeger, loki, kubectl).

Same transport as clients.jev_diag.collector.fetch_alerts_via_mcp: fastmcp Client over SSE at
http://{API_HOSTNAME}:{MCP_SERVER_PORT}/<server>/sse. Server-side failures come back as text starting with
"[prom_mcp] Error" / "[ob_mcp] Error"; both those and transport failures raise McpError.
"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger("all.jev_agent.mcp")

SERVERS = ("prometheus", "jaeger", "loki", "kubectl")
_ERROR_PREFIXES = ("[prom_mcp] Error", "[ob_mcp] Error", "[loki_mcp] Error", "Error executing tool")


class McpError(RuntimeError):
    """The MCP call failed (transport, tool error, or an error string returned by the server)."""


class McpQueryError(McpError):
    """The server is up but rejected the query itself (HTTP 400/422 from Prometheus): not an availability failure."""


_QUERY_REJECTED_RE = re.compile(r"\b(400|422) Client Error")


def mcp_url(server: str) -> str:
    """SSE URL of an SREGym MCP server (prometheus, jaeger, loki, ...) from API_HOSTNAME and MCP_SERVER_PORT."""
    host = os.getenv("API_HOSTNAME", "localhost")
    port = os.getenv("MCP_SERVER_PORT", "9954")
    return f"http://{host}:{port}/{server}/sse"


def _result_text(result: Any) -> str:
    """Text of a call_tool result across fastmcp versions (list of content parts, or CallToolResult)."""
    parts = getattr(result, "content", result)
    if isinstance(parts, str):
        return parts
    try:
        return "\n".join(getattr(part, "text", "") or "" for part in parts)
    except TypeError:
        return str(parts)


async def _call_async(server: str, tool: str, args: dict, timeout: float) -> str:
    """Call one tool on an MCP server over SSE and return its text; McpError when the result is an error."""
    from fastmcp import Client
    from fastmcp.client import SSETransport

    async with Client(SSETransport(url=mcp_url(server), sse_read_timeout=timeout)) as client:
        result = await client.call_tool(tool, arguments=args, timeout=timeout)
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            raise McpError(f"{server}.{tool} returned an error: {_result_text(result)[:300]}")
        return _result_text(result)


def _run_sync(coro_factory, timeout: float) -> str:
    """asyncio.run, also when the caller already runs inside an event loop (then in a worker thread)."""

    def _go() -> str:
        return asyncio.run(asyncio.wait_for(coro_factory(), timeout=timeout + 5))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _go()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_go).result()


def call_mcp(server: str, tool: str, args: dict, timeout: float = 15.0) -> str:
    """Call one MCP tool and return its text. Raises McpError on any failure or an error-string result."""
    if server not in SERVERS:
        raise McpError(f"unknown MCP server {server!r}")
    try:
        text = _run_sync(lambda: _call_async(server, tool, dict(args or {}), timeout), timeout)
    except McpError:
        raise
    except Exception as exc:  # noqa: BLE001 - every transport/tool failure becomes McpError
        raise McpError(f"{server}.{tool} failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
    stripped = (text or "").lstrip()
    if stripped.startswith(_ERROR_PREFIXES):
        cls = McpQueryError if _QUERY_REJECTED_RE.search(stripped[:400]) else McpError
        raise cls(f"{server}.{tool}: {stripped[:300]}")
    return text


def parse_py_literal(text: str | None) -> Any:
    """Parse the Python-repr payloads the MCP servers return (str(dict)), falling back to JSON. None if neither."""
    if text is None:
        return None
    text = text.strip()
    if not text or text == "None":
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    try:
        return json.loads(text)
    except ValueError:
        return None


def preflight(timeout: float = 10) -> bool:
    """True when the Prometheus MCP server answers a trivial query."""
    try:
        data = parse_py_literal(call_mcp("prometheus", "get_metrics", {"query": "vector(1)"}, timeout=timeout))
    except McpError as exc:
        logger.warning("MCP preflight failed: %s", exc)
        return False
    return isinstance(data, dict) and "result" in data
