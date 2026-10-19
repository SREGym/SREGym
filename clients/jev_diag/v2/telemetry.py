"""Telemetry facts for an investigated component, read with the team's jev_agent tools: Prometheus series
(CPU, memory, request rate, errors and latency from span metrics) and the calls observed to and from it (traces,
else span metrics). Each line is a short fact with its numbers; telemetry is optional, so any failure yields no
lines. The MCP server is checked once per process; when it cannot be reached, telemetry is off for the run.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("all.jev_diag.v2.telemetry")

FAMILIES = ("cpu", "memory", "rpc")
MAX_LINES_PER_SOURCE = 3
_SKIP_PREFIXES = ("no ", "(pods started", "0 quer")
_available: bool | None = None


def enabled() -> bool:
    global _available
    if os.environ.get("JEV_DIAG_V2_TELEMETRY", "1") == "0":
        return False
    if _available is None:
        try:
            from clients.jev_agent import mcp

            _available = bool(mcp.preflight(timeout=10))
        except Exception as exc:  # noqa: BLE001 - no MCP server: no telemetry
            logger.info("telemetry off: %s", exc)
            _available = False
    return _available


CALL_GRAPH_MINUTES = 15


def observed_calls() -> list[dict]:
    """The tracing backend's call graph over the last minutes: [{"parent", "child", "calls"}] by service name
    (empty when telemetry is unavailable)."""
    if not enabled():
        return []
    from clients.jev_agent import mcp

    try:
        text = mcp.call_mcp("jaeger", "get_dependency_graph", {"last_n_minutes": CALL_GRAPH_MINUTES})
        data = mcp.parse_py_literal(text)
    except Exception as exc:  # noqa: BLE001 - no call graph: the other dependency sources remain
        logger.info("call graph: %s", exc)
        return []
    items = data.get("data") if isinstance(data, dict) else data
    return [
        {"parent": e.get("parent"), "child": e.get("child"), "calls": int(e.get("callCount") or 0)}
        for e in items or []
        if isinstance(e, dict) and e.get("parent") and e.get("child")
    ]


def component_lines(snapshot, cid: str) -> list[str]:
    """Metric and call-edge facts for one component (empty when telemetry is unavailable)."""
    if not enabled():
        return []
    from clients.jev_agent import metrics
    from clients.jev_agent.tools.call_edges import run_call_edges
    from clients.jev_agent.types import AgentContext

    ctx = AgentContext(
        app_info=snapshot.app, namespaces=list(snapshot.app.get("namespaces") or []), snapshot=snapshot, overview={}
    )
    lines: list[str] = []
    for family in FAMILIES:
        try:
            result = metrics.metric_lines(ctx, cid, family)
        except Exception as exc:  # noqa: BLE001 - one family failing leaves the others
            logger.info("metrics %s for %s: %s", family, cid, exc)
            continue
        kept = [line for line in result.lines if not line.startswith(_SKIP_PREFIXES)]
        lines += [f"{family} metrics: {line}" for line in kept[:MAX_LINES_PER_SOURCE]]
    try:
        result = run_call_edges(ctx, cid)
        lines += [f"observed calls: {line}" for line in result.lines[:MAX_LINES_PER_SOURCE]]
    except Exception as exc:  # noqa: BLE001
        logger.info("call edges for %s: %s", cid, exc)
    return lines
