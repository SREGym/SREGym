"""call_edges: observed calls to and from a component with error ratios (traces, else span metrics)."""

from __future__ import annotations

import re

from clients.jev_agent import mcp, metrics
from clients.jev_agent.overview import neighbors
from clients.jev_agent.tools._common import _comp, _components
from clients.jev_agent.types import AgentContext, ToolResult

EDGE_WINDOW_MINUTES = 15


def _service_owner_index(comps: dict[str, dict]) -> dict[str, str]:
    """{lowercased service name: component id} over the components' Services."""
    index: dict[str, str] = {}
    for cid, comp in comps.items():
        for name in metrics.service_names(comp):
            index.setdefault(name.lower(), cid)
    return index


def _span_error_ratios(ctx: AgentContext, names: list[str]) -> dict[str, tuple[float, float]]:
    """{service: (spans in the window, error ratio)} from span metrics."""
    call = ctx.extra.get("prom_call") or metrics.prom_call()
    label = metrics.ensure_span_label(ctx, call)
    if not label or not names:
        return {}
    regex = "|".join(re.escape(n).replace("\\", "\\\\") for n in names)
    total_q = f'sum by ({label})(increase({metrics.SPAN_CALLS}{{{label}=~"{regex}"}}[{EDGE_WINDOW_MINUTES}m]))'
    err_q = (
        f'sum by ({label})(increase({metrics.SPAN_CALLS}{{{label}=~"{regex}",status_code="STATUS_CODE_ERROR"}}'
        f"[{EDGE_WINDOW_MINUTES}m]))"
    )
    totals = {s["labels"].get(label): s["points"][-1][1] for s in metrics.parse_prom(call(total_q)) if s["points"]}
    errors = {s["labels"].get(label): s["points"][-1][1] for s in metrics.parse_prom(call(err_q)) if s["points"]}
    return {name: (total, (errors.get(name, 0.0) / total) if total else 0.0) for name, total in totals.items()}


def run_call_edges(ctx: AgentContext, cid: str) -> ToolResult:
    """call_edges: who calls the component and whom it calls, with error ratios (traces, else span metrics)."""
    comps = _components(ctx)
    comp = _comp(ctx, cid)
    index = _service_owner_index(comps)
    own_names = {n.lower() for n in metrics.service_names(comp)}
    edges: list[dict] = []
    jaeger_error = None
    try:
        text = mcp.call_mcp("jaeger", "get_dependency_graph", {"last_n_minutes": EDGE_WINDOW_MINUTES})
        data = mcp.parse_py_literal(text)
        items = data.get("data") if isinstance(data, dict) else data
        for e in items or []:
            if not isinstance(e, dict):
                continue
            parent, child = e.get("parent") or "", e.get("child") or ""
            if (
                index.get(parent.lower()) == cid
                or index.get(child.lower()) == cid
                or parent.lower() in own_names
                or child.lower() in own_names
            ):
                edges.append({"parent": parent, "child": child, "calls": int(e.get("callCount") or 0)})
    except mcp.McpError as exc:
        jaeger_error = str(exc)
    edges.sort(key=lambda e: -e["calls"])
    involved = sorted({e["parent"] for e in edges} | {e["child"] for e in edges} | set(metrics.service_names(comp)))
    if not edges:
        involved = sorted(
            set(metrics.service_names(comp)) | {comps[n]["name"] for n in neighbors(ctx.overview, cid) if n in comps}
        )
    try:
        ratios = _span_error_ratios(ctx, involved)
    except mcp.McpQueryError:
        ratios = {}
    lines = []
    for e in edges[:6]:
        text = f"{e['parent']} -> {e['child']}: {e['calls']} calls/{EDGE_WINDOW_MINUTES}m"
        if e["child"] in ratios:
            total, ratio = ratios[e["child"]]
            text += f"; {e['child']} spans {ratio:.0%} errors ({total:.0f} spans)"
        lines.append(text)
    if not edges:
        source = "Jaeger unavailable" if jaeger_error else "no Jaeger edges in the window"
        lines.append(f"{source}; span-metric totals for {cid} and its configured neighbors follow")
        for name in involved:
            if name in ratios:
                total, ratio = ratios[name]
                lines.append(f"{name}: {total:.0f} spans/{EDGE_WINDOW_MINUTES}m, {ratio:.0%} errors (span metrics)")
            else:
                lines.append(f"{name}: no span metrics in the last {EDGE_WINDOW_MINUTES}m")
    return ToolResult(lines=lines, raw={"edges": edges, "ratios": ratios, "jaeger_error": jaeger_error})
