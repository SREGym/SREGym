"""events: Kubernetes events of a scope grouped by type and reason, start-up history kept apart."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from clients.jev_agent.tools import _common
from clients.jev_agent.tools._common import MESSAGE_CHARS, _ago, _comp, _components, _namespaces
from clients.jev_agent.types import MAX_FINDINGS, AgentContext, ToolResult
from clients.jev_diag import collector
from clients.jev_diag.timeutil import parse_time

logger = logging.getLogger("all.jev_agent.tools")

SCOPES = ("namespace", "kube-system")


def _scope_applicable(ctx: AgentContext, scope: str) -> bool:
    """Whether the scope is 'namespace', 'kube-system' or a component id."""
    return scope in SCOPES or scope in _components(ctx)


def _ready_since(pod: dict) -> datetime | None:
    """When a raw pod last became Ready, or None if it is not Ready now."""
    for c in (pod.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return parse_time(c.get("lastTransitionTime")) if c.get("status") == "True" else None
    return None


def _event_line(etype: str, reason: str, kind: str | None, g: dict) -> str:
    """One line for an event group: type, reason, count, objects, last time and latest message."""
    return (
        f"{etype} {reason} x{g['count']} on {len(g['objects'])} {kind or 'object'}(s) "
        f"({', '.join(str(o) for o in g['objects'][:2])}), last {_ago(g['last'])}: {collector.trim(g['message'], MESSAGE_CHARS)}"
    )


def _component_events(items: list[dict], comps: dict[str, dict], target: str) -> list[dict]:
    """The events whose object is the target component or maps to it (its pods, ReplicaSets, ...)."""
    comp = comps[target]

    def owned(ev: dict) -> bool:
        """Whether the event's object is the component or maps to it."""
        obj = ev.get("involvedObject") or {}
        if obj.get("name") == comp["name"]:
            return True
        try:
            return (
                collector.component_for_object(
                    obj.get("kind", ""), obj.get("name", ""), obj.get("namespace") or ev["_ns"], comps
                )
                == target
            )
        except Exception as exc:  # noqa: BLE001 - an unmappable object is not this component's
            logger.debug("event object %s not mapped to a component: %s", obj, exc)
            return False

    return [e for e in items if owned(e)]


def _group_events(items: list[dict], pods: dict, pods_known: bool) -> tuple[dict, dict, dict]:
    """(current, start-up, gone) event groups keyed by (type, reason, object kind), each with its count, objects,
    last time and latest message. Pod events from before the pod became Ready are start-up history, and events of
    pods no longer listed are gone."""
    current: dict[tuple, dict] = {}
    startup: dict[tuple, dict] = {}
    gone: dict[tuple, dict] = {}
    for ev in items:
        obj = ev.get("involvedObject") or {}
        last = collector._event_time(ev)
        bucket = current
        if obj.get("kind") == "Pod" and pods_known:
            pod = pods.get((obj.get("namespace") or ev["_ns"], obj.get("name")))
            if pod is None:
                bucket = gone
            else:
                ready = _ready_since(pod)
                seen = parse_time(last)
                if ready and seen and seen <= ready:
                    bucket = startup
        key = (ev.get("type"), ev.get("reason"), obj.get("kind"))
        g = bucket.setdefault(key, {"count": 0, "objects": [], "last": "", "message": ""})
        g["count"] += int(ev.get("count") or (ev.get("series") or {}).get("count") or 1)
        if obj.get("name") not in g["objects"]:
            g["objects"].append(obj.get("name"))
        if last >= g["last"]:
            g["last"], g["message"] = last, ev.get("message") or ""
    return current, startup, gone


def run_events(ctx: AgentContext, scope: str) -> ToolResult:
    """Events grouped by type/reason. Pod events from before a pod (Ready now) became Ready are start-up history,
    and events of pods that no longer exist are kept apart, as jev_diag's pod_details / attach_warning_events do."""
    comps = _components(ctx)
    target = None
    if scope == "namespace":
        nss = _namespaces(ctx)
    elif scope == "kube-system":
        nss = ["kube-system"]
    else:
        comp = _comp(ctx, scope)
        nss, target = [comp["namespace"]], scope
    items, pods = [], {}
    pods_known = True
    for ns in nss:
        items += [{**e, "_ns": ns} for e in _common._items("events", ns)]
        try:
            pods.update({(ns, (p.get("metadata") or {}).get("name")): p for p in _common._items("pods", ns)})
        except Exception:  # noqa: BLE001 - without the pod list every event is shown as current
            pods_known = False
    if target:
        items = _component_events(items, comps, target)
    current, startup, gone = _group_events(items, pods, pods_known)
    types = Counter(ev.get("type") for ev in items)
    lines = [
        f"{len(items)} events in {', '.join(nss)}{' for ' + scope if target else ''}: "
        + ", ".join(f"{n} {t}" for t, n in types.most_common())
    ]
    ordered = sorted(current.items(), key=lambda kv: (kv[0][0] == "Warning", kv[1]["last"]), reverse=True)
    for (etype, reason, kind), g in ordered[: MAX_FINDINGS - 3]:
        lines.append(_event_line(etype, reason, kind, g))
    for title, groups in (
        ("start-up history (before the pods became Ready, which they are now)", startup),
        ("events of pods no longer present", gone),
    ):
        warn = [(k, g) for k, g in groups.items() if k[0] == "Warning"]
        if warn:
            warn.sort(key=lambda kv: kv[1]["last"], reverse=True)
            lines.append(
                f"{title}: "
                + "; ".join(f"{k[1]} x{g['count']} on {len(g['objects'])} {k[2] or 'object'}(s)" for k, g in warn[:4])
            )
    return ToolResult(lines=lines)
