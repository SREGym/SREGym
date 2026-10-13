"""Shared contracts between the tool layer (overview, tools, mcp, metrics) and the policy loop.

Rule for everything under clients/jev_agent/: tools return facts and generic statistics only. No code path may
name or detect a specific fault (no "this looks like X" logic), and nothing may call jev_diag's fault-specific
checks (checks.local_traffic_policy_gaps, shared_rwo_claims, admission_changes, stuck_terminating,
link_stuck_objects, coredns_service_overrides, derive.server_login_rejections, derive.post_process, ...).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Target kinds a tool can take.
COMPONENT = "component"  # a workload id such as "deployment/frontend" (keys of snapshot.components)
CLUSTER_GROUP = "cluster_group"  # one of CLUSTER_GROUPS
EDGE = "edge"  # "client_cid->target_cid"
SCOPE = "scope"  # "namespace" | "kube-system" | a component id
OBJECT = "object"  # a non-workload object: "<kind>/<namespace>/<name>" or "<kind>/<name>" (cluster-scoped)

CLUSTER_GROUPS = ("admission", "quota", "network", "rbac", "dns", "nodes", "storage")

MAX_FINDINGS = 12  # lines per tool result
MAX_LINE_CHARS = 400  # long enough for a Kubernetes event or condition message after its prefix


@dataclass
class ToolResult:
    """What a tool returns. `lines` are short factual findings (<= MAX_FINDINGS, each <= MAX_LINE_CHARS)."""

    lines: list[str]
    error: str | None = None  # set when the tool could not run; lines may then hold a one-line explanation
    raw: dict | None = None  # optional structured payload, kept in the tool cache only (never sent to the model)


@dataclass
class ToolSpec:
    """A tool on the menu: what it reveals, how to run it, and on which targets it applies."""

    name: str
    target_kind: str  # COMPONENT | CLUSTER_GROUP | EDGE | SCOPE
    reveals: str  # one sentence: what the model learns by choosing this tool (used in option descriptions)
    run: Callable[[AgentContext, str], ToolResult]
    applicable: Callable[[AgentContext, str], bool] = lambda ctx, target: True
    uses_mcp: bool = False


@dataclass
class AgentContext:
    """Everything tools need. Built once by the driver after the start delay."""

    app_info: dict  # from conductor GET /get_app: name, description, namespaces
    namespaces: list[str]
    snapshot: (
        Any  # clients.jev_diag.collector.ClusterSnapshot (full collect_snapshot output; tools may read raw fields)
    )
    overview: dict  # overview.build_overview(snapshot): the ONLY whole-cluster view shown to the model
    mcp_ok: bool = True  # set False after repeated MCP failures; MCP tools then become inapplicable
    mcp_failures: int = 0
    span_label: str | None = None  # label used to filter span metrics by service ("service_name" or None if absent)
    cache: Any = None  # tools.ToolCache or None
    extra: dict = field(default_factory=dict)


@dataclass
class Observation:
    """One executed action, as kept in the notebook."""

    id: str  # "O1", "O2", ...
    step: int
    tool: str
    target: str
    lines: list[str]
    error: str | None = None
    abnormal: float | None = None  # model's P(abnormal), filled on the following step
