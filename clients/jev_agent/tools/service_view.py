"""service_view: whether a component's Services select its pods, with endpoints and ports."""

from __future__ import annotations

from clients.jev_agent.tools import _common
from clients.jev_agent.tools._common import _comp, _components, _owner_of, _when
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag import changes as change_log
from clients.jev_diag import collector
from clients.jev_diag.checks import json_short


def _service_lines(
    ctx: AgentContext, svc: dict, ep: dict | None, ns_pods: list[dict], comps: dict[str, dict]
) -> list[str]:
    """Three lines for a Service: its ports, selector and traffic settings; its endpoints and the pods its selector
    matches; its creation and later writes."""
    meta, spec = svc.get("metadata") or {}, svc.get("spec") or {}
    ports = ", ".join(
        f"{p.get('name') + ' ' if p.get('name') else ''}{p.get('port')}->{p.get('targetPort')}/{p.get('protocol', 'TCP')}"
        for p in spec.get("ports") or []
    )
    extras = [
        f"{k} {spec[k]}"
        for k in ("internalTrafficPolicy", "externalTrafficPolicy", "sessionAffinity", "publishNotReadyAddresses")
        if spec.get(k) not in (None, "Cluster", "None", False)
    ]
    first = (
        f"Service {meta.get('name')} ({spec.get('type')} {spec.get('clusterIP')}): ports {ports or 'none'}; selector "
        f"{json_short(spec.get('selector') or {}, 100)}" + (f"; {', '.join(extras)}" if extras else "")
    )
    ready, not_ready, ep_ports = [], [], set()
    for sub in (ep or {}).get("subsets") or []:
        ready += [a.get("targetRef", {}).get("name") or a.get("ip") for a in sub.get("addresses") or []]
        not_ready += [a.get("targetRef", {}).get("name") or a.get("ip") for a in sub.get("notReadyAddresses") or []]
        ep_ports |= {f"{p.get('port')}/{p.get('protocol', 'TCP')}" for p in sub.get("ports") or []}
    selector = spec.get("selector") or {}
    matched = []
    if selector:
        for pod in ns_pods:
            if collector.selector_matches(selector, (pod.get("metadata") or {}).get("labels")):
                matched.append(f"{pod['metadata']['name']}({_owner_of(comps, pod) or 'no workload'})")
    second = (
        f"endpoints of {meta.get('name')}: ready {len(ready)} {ready[:3] if ready else ''}, notReady {len(not_ready)}"
        f"{' ' + str(not_ready[:3]) if not_ready else ''}; endpoint ports {sorted(ep_ports) or 'none'}; selector "
        f"matches {len(matched)} pod(s): {', '.join(matched[:4]) or 'none'}"
    )
    # the Service's own change history (generic managedFields reading, same as for workloads)
    history = f"Service {meta.get('name')} created {_when(ctx, meta.get('creationTimestamp'))}"
    later = change_log.later_writes(meta)
    if later:
        history += "; later writes: " + "; ".join(
            f"{_when(ctx, w['at'])} set {', '.join(w['fields'][:5])}" for w in later[-2:]
        )
    else:
        history += "; no separate write after creation"
    return [first, second, history]


def run_service_view(ctx: AgentContext, cid: str) -> ToolResult:
    """service_view: whether the Services of the component select its pods, with endpoints and ports."""
    comp = _comp(ctx, cid)
    comps = _components(ctx)
    ns = comp["namespace"]
    services = _common._items("services", ns, managed=True)
    endpoints = {(e.get("metadata") or {}).get("name"): e for e in _common._items("endpoints", ns)}
    ns_pods = _common._ns_pods(ns)
    own_pods = [p for p in ns_pods if _owner_of(comps, p) == cid]
    own_labels = [(p.get("metadata") or {}).get("labels") or {} for p in own_pods]
    known = {s.get("name") for s in comp.get("services") or []} | {comp["name"]}
    relevant = []
    for svc in services:
        name, selector = svc["metadata"].get("name"), (svc.get("spec") or {}).get("selector")
        selects_own = bool(selector) and any(collector.selector_matches(selector, lab) for lab in own_labels)
        if name in known or selects_own:
            relevant.append(svc)
    lines: list[str] = []
    for svc in relevant[:3]:
        lines += _service_lines(ctx, svc, endpoints.get(svc["metadata"].get("name")), ns_pods, comps)
    ports = [
        f"{c.get('name')}: {c.get('ports') or 'none declared'}"
        for c in comp.get("containers") or []
        if not c.get("init")
    ]
    lines.append(f"{cid} container ports: " + "; ".join(ports))
    if own_labels:
        lines.append(f"{cid} pod labels: {json_short(own_labels[0], 160)} ({len(own_pods)} pod(s))")
    if not relevant:
        lines.insert(0, f"no Service in {ns} is named after {comp['name']} or selects its pods")
    return ToolResult(lines=lines, raw={"services": [s["metadata"].get("name") for s in relevant]})
