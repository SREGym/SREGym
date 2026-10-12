"""metric_series_*: a component's Prometheus series, one tool per metrics family."""

from __future__ import annotations

from collections.abc import Callable

from clients.jev_agent import metrics
from clients.jev_agent.types import AgentContext, ToolResult


def _metric_runner(family: str) -> Callable[[AgentContext, str], ToolResult]:
    """A tool run function returning one metrics family's lines for a component."""

    def _run(ctx: AgentContext, cid: str) -> ToolResult:
        return metrics.metric_lines(ctx, cid, family)

    return _run
