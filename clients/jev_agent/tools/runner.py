"""Running a tool on a target (never raises) and listing the targets each tool can take now."""

from __future__ import annotations

import logging
import time

from clients.jev_agent import mcp
from clients.jev_agent.overview import component_scores
from clients.jev_agent.tools._common import _components
from clients.jev_agent.tools.events import SCOPES
from clients.jev_agent.tools.objects import object_targets
from clients.jev_agent.tools.registry import REGISTRY
from clients.jev_agent.types import (
    CLUSTER_GROUP,
    CLUSTER_GROUPS,
    COMPONENT,
    EDGE,
    MAX_FINDINGS,
    MAX_LINE_CHARS,
    OBJECT,
    SCOPE,
    AgentContext,
    ToolResult,
    ToolSpec,
)

logger = logging.getLogger("all.jev_agent.tools")

MCP_FAILURE_LIMIT = 2
TOP_EDGE_COMPONENTS = 6


def candidate_targets(ctx: AgentContext, spec: ToolSpec) -> list[str]:
    """Targets the tool can take now, most promising first (components by generic abnormality score)."""
    scores = component_scores(ctx.overview)
    comps = [c for c in sorted(scores, key=lambda c: (-scores[c], c)) if c in _components(ctx)]
    if spec.target_kind == COMPONENT:
        targets = comps
    elif spec.target_kind == EDGE:
        overview_comps = ctx.overview.get("components") or {}
        pairs: list[tuple[float, str]] = []
        for cid in comps[:TOP_EDGE_COMPONENTS]:
            view = overview_comps.get(cid) or {}
            for dep in view.get("depends_on") or []:
                pairs.append((max(scores.get(cid, 0), scores.get(dep, 0)), f"{cid}->{dep}"))
            for client in view.get("clients") or []:
                pairs.append((max(scores.get(cid, 0), scores.get(client, 0)), f"{client}->{cid}"))
        pairs.sort(key=lambda p: -p[0])
        targets = list(dict.fromkeys(t for _, t in pairs))
    elif spec.target_kind == CLUSTER_GROUP:
        targets = list(CLUSTER_GROUPS)
    elif spec.target_kind == SCOPE:
        targets = list(SCOPES)
    elif spec.target_kind == OBJECT:
        targets = object_targets(ctx)
    else:
        targets = []
    out = []
    for t in targets:
        try:
            if spec.applicable(ctx, t):
                out.append(t)
        except Exception as exc:  # noqa: BLE001 - an applicability check must not break the menu
            logger.info("applicability of %s on %s failed: %s", spec.name, t, exc)
            continue
    return out


def _clip(lines: list[str]) -> list[str]:
    """The first MAX_FINDINGS lines, whitespace collapsed and each cut to MAX_LINE_CHARS."""
    out = []
    for line in lines[:MAX_FINDINGS]:
        text = " ".join(str(line).split())
        out.append(text if len(text) <= MAX_LINE_CHARS else text[: MAX_LINE_CHARS - 1] + "…")
    return out


def run(ctx: AgentContext, name: str, target: str) -> ToolResult:
    """Run one tool on one target. Never raises; failures come back as a one-line ToolResult with `error` set."""
    spec = REGISTRY.get(name)
    cache = ctx.cache
    if cache is not None and getattr(cache, "replay", False):
        cached = cache.lookup(name, target)
        if cached is None:
            return ToolResult(lines=[f"{name}({target}) failed: not in the replay cache"], error="not recorded")
        return cached
    if spec is None:
        return ToolResult(lines=[f"{name}({target}) failed: unknown tool"], error="unknown tool")
    try:
        applicable = spec.applicable(ctx, target)
    except Exception:  # noqa: BLE001 - a broken applicability check means "not applicable"
        applicable = False
    if not applicable:
        result = ToolResult(lines=[f"{name}({target}) is not applicable"], error="not applicable")
    else:
        started = time.monotonic()
        try:
            result = spec.run(ctx, target)
            if spec.uses_mcp:
                ctx.mcp_failures = 0
        except mcp.McpQueryError as exc:
            result = ToolResult(lines=[f"{name}({target}) failed: query rejected: {exc}"], error=f"query: {exc}")
        except mcp.McpError as exc:
            ctx.mcp_failures += 1
            if ctx.mcp_failures >= MCP_FAILURE_LIMIT:
                ctx.mcp_ok = False
            result = ToolResult(lines=[f"{name}({target}) failed: {exc}"], error=f"mcp: {exc}")
        except Exception as exc:  # noqa: BLE001 - one failed tool must not stop the loop
            logger.info("tool %s(%s) failed", name, target, exc_info=True)
            result = ToolResult(lines=[f"{name}({target}) failed: {type(exc).__name__}: {exc}"], error=str(exc))
        logger.debug("tool %s(%s) took %.1fs", name, target, time.monotonic() - started)
    result.lines = _clip(result.lines or [f"{name}({target}): no data"])
    if cache is not None:
        try:
            cache.record(name, target, result)
        except Exception:  # noqa: BLE001 - caching is best effort
            logger.warning("tool cache write failed", exc_info=True)
    return result
