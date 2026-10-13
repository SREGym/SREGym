"""The tool menu: one ToolSpec per generic tool, with what it reveals and where it applies."""

from __future__ import annotations

from clients.jev_agent import metrics
from clients.jev_agent.tools._common import _components
from clients.jev_agent.tools.backend_probe import _backend_applicable, run_backend_probe
from clients.jev_agent.tools.call_edges import run_call_edges
from clients.jev_agent.tools.client_check import _edge_applicable, run_client_check
from clients.jev_agent.tools.cluster_objects import run_cluster_objects
from clients.jev_agent.tools.config_changes import run_config_and_changes
from clients.jev_agent.tools.describe import run_describe
from clients.jev_agent.tools.events import _scope_applicable, run_events
from clients.jev_agent.tools.logs import run_logs
from clients.jev_agent.tools.metric_series import _metric_runner
from clients.jev_agent.tools.objects import run_object_view, run_related_objects
from clients.jev_agent.tools.service_view import run_service_view
from clients.jev_agent.types import (
    CLUSTER_GROUP,
    CLUSTER_GROUPS,
    COMPONENT,
    EDGE,
    OBJECT,
    SCOPE,
    AgentContext,
    ToolSpec,
)


def _is_component(ctx: AgentContext, target: str) -> bool:
    """Whether the target is a component id."""
    return target in _components(ctx)


def _mcp_component(ctx: AgentContext, target: str) -> bool:
    """Whether the target is a component id and MCP (Prometheus, Jaeger) is up."""
    return ctx.mcp_ok and target in _components(ctx)


def _is_group(ctx: AgentContext, target: str) -> bool:
    """Whether the target is one of the cluster object groups."""
    return target in CLUSTER_GROUPS


_METRIC_REVEALS = {
    "cpu": "15-minute CPU usage and CPU throttling series for the component's containers, with their limits",
    "memory": "15-minute memory working-set series for the component's containers versus their memory limits",
    "restarts": "container restart counts over 15 minutes plus last-terminated and waiting reasons from metrics",
    "rpc": "15-minute request rate, error ratio and p95 latency of the component's service from span metrics",
    "probe": "15-minute blackbox probe success history for the component's Service endpoints",
    "app": "the component's own application metrics whose values changed most over 15 minutes",
}

REGISTRY: dict[str, ToolSpec] = {
    "describe": ToolSpec(
        name="describe",
        target_kind=COMPONENT,
        reveals="live spec and state of the workload: pod and container states, events, image, args, env values, "
        "probes, resources, volumes and scheduling fields",
        run=run_describe,
        applicable=_is_component,
    ),
    "logs": ToolSpec(
        name="logs",
        target_kind=COMPONENT,
        reveals="recent log error signatures with counts and timing, plus the last lines of crashed containers",
        run=run_logs,
        applicable=_is_component,
    ),
    "service_view": ToolSpec(
        name="service_view",
        target_kind=COMPONENT,
        reveals="the component's Services: ports and targetPorts, selectors, ready/not-ready endpoints and which "
        "pods the selectors match, next to the containers' declared ports",
        run=run_service_view,
        applicable=_is_component,
    ),
    **{
        f"metric_series_{family}": ToolSpec(
            name=f"metric_series_{family}",
            target_kind=COMPONENT,
            reveals=_METRIC_REVEALS[family],
            run=_metric_runner(family),
            applicable=_mcp_component,
            uses_mcp=True,
        )
        for family in metrics.FAMILIES
    },
    "call_edges": ToolSpec(
        name="call_edges",
        target_kind=COMPONENT,
        reveals="observed calls to and from the component over 15 minutes (trace dependency edges with call "
        "counts) and per-service span error ratios",
        run=run_call_edges,
        applicable=_mcp_component,
        uses_mcp=True,
    ),
    "config_and_changes": ToolSpec(
        name="config_and_changes",
        target_kind=COMPONENT,
        reveals="who wrote the workload spec and when, pod template revision diffs, referenced ConfigMap contents "
        "and Secret key names, timed relative to symptom onset",
        run=run_config_and_changes,
        applicable=_is_component,
    ),
    "cluster_objects": ToolSpec(
        name="cluster_objects",
        target_kind=CLUSTER_GROUP,
        reveals="cluster-level objects of one group (admission webhooks, quotas, network policies, RBAC, cluster "
        "DNS, nodes or storage) with their state and creation/modification times",
        run=run_cluster_objects,
        applicable=_is_group,
    ),
    "backend_probe": ToolSpec(
        name="backend_probe",
        target_kind=COMPONENT,
        reveals="a read-only health command run inside the data store or broker (ping, readiness, or two "
        "consumer-group samples showing partition progress)",
        run=run_backend_probe,
        applicable=_backend_applicable,
    ),
    "client_check": ToolSpec(
        name="client_check",
        target_kind=EDGE,
        reveals="from inside a client pod: what the dependency's Service name resolves to and whether a TCP "
        "connection to its port succeeds",
        run=run_client_check,
        applicable=_edge_applicable,
    ),
    "object_view": ToolSpec(
        name="object_view",
        target_kind=OBJECT,
        reveals="one non-workload object as it is now (ConfigMap data, a webhook's rules and backend, a quota's limits, "
        "a policy's spec, a custom resource), with when it was created and last written; Secret values are never read",
        run=run_object_view,
    ),
    "related_objects": ToolSpec(
        name="related_objects",
        target_kind=COMPONENT,
        reveals="objects around the workload (ReplicaSets, claims, ConfigMaps, Secrets, ServiceAccount, role "
        "bindings, and Services, policies, budgets, autoscalers selecting it) with their failing conditions and "
        "Warning events",
        run=run_related_objects,
        applicable=_is_component,
    ),
    "events": ToolSpec(
        name="events",
        target_kind=SCOPE,
        reveals="recent Kubernetes events of all types for the scope, grouped by reason, Warning groups first, newest first",
        run=run_events,
        applicable=_scope_applicable,
    ),
}
