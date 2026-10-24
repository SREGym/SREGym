"""Telemetry facts for an investigated component, read with the team's jev_agent tools: Prometheus series
(CPU, memory, request rate, errors and latency from span metrics) and the calls observed to and from it (traces,
else span metrics). Each line is a short fact with its numbers; telemetry is optional, so any failure yields no
lines. The MCP server is checked once per process; when it cannot be reached, telemetry is off for the run.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("all.jev_diag.v1_1.telemetry")

FAMILIES = ("cpu", "memory", "rpc")
MAX_LINES_PER_SOURCE = 3
# Saturation judged on its own scale, not only against a baseline that may already lie inside the fault (USE method).
CPU_THROTTLED_SHARE = 0.25  # the threshold of kube-prometheus's CPUThrottlingHigh alert (share of CFS periods)
MEMORY_LIMIT_SHARE = 0.9  # working set within a tenth of its limit
_SKIP_PREFIXES = ("no ", "(pods started", "0 quer")
_available: bool | None = None


def enabled() -> bool:
    global _available
    if os.environ.get("JEV_DIAG_V1_1_TELEMETRY", "1") == "0":
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
        lines += saturation(family, getattr(result, "raw", None) or {})  # before any cap: the most decisive fact
        kept = [line for line in result.lines if not line.startswith(_SKIP_PREFIXES)]
        lines += [f"{family} metrics: {line}" for line in kept[:MAX_LINES_PER_SOURCE]]
    try:
        result = run_call_edges(ctx, cid)
        lines += [f"observed calls: {line}" for line in result.lines[:MAX_LINES_PER_SOURCE]]
    except Exception as exc:  # noqa: BLE001
        logger.info("call edges for %s: %s", cid, exc)
    return lines


def saturation(family: str, raw: dict) -> list[str]:
    """Containers saturated on an absolute scale, from the summarized series: CPU-throttled in at least
    CPU_THROTTLED_SHARE of CFS periods (throttling happens well below the limit on average), or a memory working
    set that reached MEMORY_LIMIT_SHARE of its limit."""
    out = []
    if family == "cpu":
        for s in raw.get("throttled") or []:
            share = (s.get("summary") or {}).get("recent")
            if share is not None and share >= CPU_THROTTLED_SHARE:
                container = (s.get("labels") or {}).get("container")
                out.append(f"saturation: container {container} is CPU-throttled in {share:.0%} of CFS periods")
    elif family == "memory":
        for s in raw.get("working_set") or []:
            peak, limit = (s.get("summary") or {}).get("max"), s.get("limit")
            if peak and limit and peak >= MEMORY_LIMIT_SHARE * limit:
                out.append(
                    f"saturation: container {s.get('container')} memory working set reached {peak / limit:.0%} of its limit"
                )
    return out
