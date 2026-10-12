"""Generic evidence tools the decision model chooses from, one "tool:target" action per step.

Every tool returns short factual lines (raw values, counts, times, generic statistics). No tool names or
detects a specific fault, and nothing here calls jev_diag's fault-specific checks (see types.py). kubectl
reads go through clients.jev_diag.collector.run_kubectl (so recording/replay work), in-pod commands through
clients.jev_diag.probes.run_in_pod, and Prometheus/Jaeger through clients.jev_agent.mcp.

One module per tool (describe, logs, service_view, metric_series, call_edges, config_changes, cluster_objects,
backend_probe, client_check, events, objects), plus the shared helpers (_common, _podexec), the menu (registry),
running a tool (runner) and the tool cache (cache). This package re-exports the names other modules and tests
use. The kubectl reads and the in-pod exec are called through their defining module (`_common._get_json(...)`,
`_podexec._exec(...)`), so a test that stubs one patches that module, not this package.
"""

from __future__ import annotations

from clients.jev_agent.tools._common import (
    MESSAGE_CHARS,
    _comp,
    _components,
    _current_pod_names,
    _ends,
    _get_json,
    _items,
    _written,
)
from clients.jev_agent.tools._podexec import ExecUnavailable, _exec, _missing_tool, _ready_pod
from clients.jev_agent.tools.backend_probe import _kafka_sample_from, run_backend_probe
from clients.jev_agent.tools.cache import ToolCache
from clients.jev_agent.tools.call_edges import run_call_edges
from clients.jev_agent.tools.client_check import _check_sources, _resolve, run_client_check
from clients.jev_agent.tools.cluster_objects import corefile_facts, run_cluster_objects
from clients.jev_agent.tools.config_changes import _stale_env_refs, run_config_and_changes
from clients.jev_agent.tools.describe import _admission_drift, _controller_failures, _template_mix, run_describe
from clients.jev_agent.tools.events import SCOPES, run_events
from clients.jev_agent.tools.logs import _components_named, _crash_words, run_logs
from clients.jev_agent.tools.objects import _elide_encoded, object_targets, run_object_view, run_related_objects
from clients.jev_agent.tools.registry import REGISTRY
from clients.jev_agent.tools.runner import MCP_FAILURE_LIMIT, candidate_targets, run
from clients.jev_agent.tools.service_view import run_service_view

__all__ = [
    "MCP_FAILURE_LIMIT",
    "MESSAGE_CHARS",
    "REGISTRY",
    "SCOPES",
    "ExecUnavailable",
    "ToolCache",
    "_admission_drift",
    "_check_sources",
    "_comp",
    "_components",
    "_components_named",
    "_controller_failures",
    "_crash_words",
    "_current_pod_names",
    "_elide_encoded",
    "_ends",
    "_exec",
    "_get_json",
    "_items",
    "_kafka_sample_from",
    "_missing_tool",
    "_ready_pod",
    "_resolve",
    "_stale_env_refs",
    "_template_mix",
    "_written",
    "candidate_targets",
    "corefile_facts",
    "object_targets",
    "run",
    "run_backend_probe",
    "run_call_edges",
    "run_client_check",
    "run_cluster_objects",
    "run_config_and_changes",
    "run_describe",
    "run_events",
    "run_logs",
    "run_object_view",
    "run_related_objects",
    "run_service_view",
]
