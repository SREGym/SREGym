"""mitmproxy addon that enforces the evaluation network policy."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from internet_policy import (
    EndpointRule,
    endpoint_host_is_allowed,
    endpoint_is_allowed,
    provider_tool_uses_internet,
    should_stream_response,
)
from mitmproxy import ctx, http

BLOCK_LOG = Path(os.environ.get("BLOCKED_REQUEST_LOG", "/state/blocked-requests.jsonl"))
POLICY_STATE = Path(os.environ.get("EGRESS_POLICY_STATE", "/state/policy.json"))


def responseheaders(flow: http.HTTPFlow) -> None:
    """Forward long-lived SSE responses instead of buffering them forever."""
    if should_stream_response(flow.response.headers.get("content-type")):
        flow.response.stream = True


def http_connect(flow: http.HTTPFlow) -> None:
    """Reject a TLS tunnel before it can connect to an unapproved host."""
    rules = _load_policy()
    host = flow.request.host
    port = flow.request.port or 443
    if not endpoint_host_is_allowed(host, port, rules):
        _block(flow, "endpoint-not-allowed")


def request(flow: http.HTTPFlow) -> None:
    if flow.request.method.upper() == "CONNECT":
        return

    if provider_tool_uses_internet(flow.request.raw_content):
        _block(flow, "provider-web-tool")
        return

    rules = _load_policy()
    host = flow.request.host
    port = flow.request.port or (443 if flow.request.scheme == "https" else 80)
    if not endpoint_is_allowed(host, port, flow.request.path, rules):
        _block(flow, "endpoint-not-allowed")


def _load_policy() -> tuple[EndpointRule, ...]:
    """Load the fixed endpoint allowlist for each request."""
    try:
        data = json.loads(POLICY_STATE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("allowed_endpoints"), list):
            raise ValueError("invalid endpoint policy")
        return tuple(EndpointRule.from_dict(item) for item in data["allowed_endpoints"])
    except (OSError, TypeError, ValueError, KeyError) as exc:
        # A missing or malformed policy must never restore open internet.
        ctx.log.error(f"Could not load evaluation network policy: {exc}")
        return ()


def _block(flow: http.HTTPFlow, reason: str) -> None:
    flow.response = http.Response.make(
        451,
        b"Request blocked by the evaluation network policy.\n",
        {"Content-Type": "text/plain; charset=utf-8"},
    )
    try:
        _record_block(flow, reason)
    except OSError as exc:
        # Audit logging must never turn a blocked request into an allowed one.
        ctx.log.error(f"Could not record blocked request: {exc}")


def _record_block(flow: http.HTTPFlow, reason: str) -> None:
    record = _request_record(flow)
    record["reason"] = reason
    BLOCK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with BLOCK_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _request_record(flow: http.HTTPFlow) -> dict[str, str | int]:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "method": flow.request.method,
        "scheme": flow.request.scheme,
        "host": flow.request.host,
        "port": flow.request.port or (443 if flow.request.scheme == "https" else 80),
        "path": flow.request.path.split("?", 1)[0],
    }
