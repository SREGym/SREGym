"""mitmproxy addon that blocks direct access to configured GitHub owners."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from internet_policy import (
    DEFAULT_BLOCKED_GITHUB_OWNERS,
    DEFAULT_BLOCKED_GITHUB_REPOSITORIES,
    DEFAULT_BLOCKED_GITHUB_REPOSITORY_IDS,
    blocked_github_owner,
    should_stream_response,
)
from mitmproxy import ctx, http

BLOCKED_OWNERS = tuple(
    owner.strip()
    for owner in os.environ.get("BLOCKED_GITHUB_OWNERS", ",".join(DEFAULT_BLOCKED_GITHUB_OWNERS)).split(",")
    if owner.strip()
)
BLOCKED_REPOSITORY_IDS = tuple(
    repository_id.strip()
    for repository_id in os.environ.get(
        "BLOCKED_GITHUB_REPOSITORY_IDS", ",".join(DEFAULT_BLOCKED_GITHUB_REPOSITORY_IDS)
    ).split(",")
    if repository_id.strip()
)
BLOCKED_REPOSITORIES = tuple(
    repository.strip()
    for repository in os.environ.get(
        "BLOCKED_GITHUB_REPOSITORIES", ",".join(DEFAULT_BLOCKED_GITHUB_REPOSITORIES)
    ).split(",")
    if repository.strip()
)
BLOCK_LOG = Path(os.environ.get("BLOCKED_REQUEST_LOG", "/state/blocked-requests.jsonl"))


def responseheaders(flow: http.HTTPFlow) -> None:
    """Forward long-lived SSE responses instead of buffering them forever."""
    if should_stream_response(flow.response.headers.get("content-type")):
        flow.response.stream = True


def request(flow: http.HTTPFlow) -> None:
    owner = None
    # A request can use a GitHub IP in the URL while retaining a GitHub
    # hostname in its HTTP Host header. Apply the policy to both values.
    for host in (flow.request.host, flow.request.headers.get("host", "")):
        owner = blocked_github_owner(
            host,
            flow.request.path,
            flow.request.raw_content,
            BLOCKED_OWNERS,
            BLOCKED_REPOSITORY_IDS,
            BLOCKED_REPOSITORIES,
        )
        if owner is not None:
            break
    if owner is None:
        return

    flow.response = http.Response.make(
        451,
        b"Request blocked by the evaluation network policy.\n",
        {"Content-Type": "text/plain; charset=utf-8"},
    )
    try:
        _record_block(flow, owner)
    except OSError as exc:
        # Audit logging must never turn a blocked request into an allowed one.
        ctx.log.error(f"Could not record blocked request: {exc}")


def _record_block(flow: http.HTTPFlow, owner: str) -> None:
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "method": flow.request.method,
        "host": flow.request.host,
        "path": flow.request.path.split("?", 1)[0],
        "blocked_owner": owner,
    }
    BLOCK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with BLOCK_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
