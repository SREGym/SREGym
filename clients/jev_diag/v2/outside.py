"""Objects outside the application's namespaces that can carry its fault, and namespaces the application reaches
through its own configuration.

* Ingresses whose backend is an application Service (missing backend, no ready endpoints, or a recent write).
* PriorityClasses used by the application's pods, or set as the cluster default.
* DaemonSets and CronJobs in kube-system (they run on every node or on a schedule and touch every workload).
* Namespaces named in the application's own addresses (`name.other-ns.svc`), read like the application's own.

Each object is listed with the components it acts on. Only an abnormal signal (a missing backend, no ready
endpoints) or a write made after the application was deployed makes it a cluster finding; its routes or settings
are context only. Earlier writes are dropped when changes are ranked.
"""

from __future__ import annotations

import re
from datetime import datetime

from clients.jev_diag.v2 import changes as change_log
from clients.jev_diag.v2.collector import kubectl_items
from clients.jev_diag.v2.derive import OBSERVABILITY_RE

# Namespaces of the cluster and of the benchmark's own infrastructure; never read as part of the application.
INFRA_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "local-path-storage", "chaos-mesh", "khaos"}
_SVC_HOST = re.compile(
    r"(?i)(?<![\w.-])(?P<svc>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.(?P<ns>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.svc\b"
)


def related_namespaces(namespaces: list[str], errors: list[str]) -> list[str]:
    """Namespaces the application's workloads address in their env (`svc.ns.svc`), telemetry services left out."""
    found: set[str] = set()
    for ns in namespaces:
        for resource in ("deployments", "statefulsets"):
            for obj in kubectl_items(resource, ns, errors, managed_fields=False):
                spec = (obj.get("spec") or {}).get("template", {}).get("spec", {})
                for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
                    for e in c.get("env") or []:
                        for m in _SVC_HOST.finditer(str(e.get("value") or "")):
                            if not OBSERVABILITY_RE.search(m.group("svc")):
                                found.add(m.group("ns"))
    return sorted(found - set(namespaces) - INFRA_NAMESPACES)


def _service_index(components: dict[str, dict]) -> dict[str, str]:
    """'ns/service' -> component id of the workload it selects."""
    index = {}
    for cid, comp in components.items():
        for svc in comp.get("services") or []:
            index.setdefault(f"{comp['namespace']}/{svc.get('name')}", cid)
    return index


def _backends(ingress: dict) -> list[tuple[str, str, str]]:
    """(host+path, service name, port) for every route of an Ingress."""
    spec = ingress.get("spec") or {}
    out = []
    default = ((spec.get("defaultBackend") or {}).get("service")) or {}
    if default.get("name"):
        port = (default.get("port") or {}).get("number") or (default.get("port") or {}).get("name") or ""
        out.append(("(default)", default["name"], str(port)))
    for rule in spec.get("rules") or []:
        for path in (rule.get("http") or {}).get("paths") or []:
            svc = (path.get("backend") or {}).get("service") or {}
            if svc.get("name"):
                port = (svc.get("port") or {}).get("number") or (svc.get("port") or {}).get("name") or ""
                out.append((f"{rule.get('host') or '*'}{path.get('path') or '/'}", svc["name"], str(port)))
    return out


def collect_outside(
    namespaces: list[str],
    components: dict[str, dict],
    pod_priority: dict[str, set[str]],
    errors: list[str],
    changes: list[dict],
    deployed: datetime | None = None,
) -> list[dict]:
    """Outside objects as findings ({kind, name, namespace, signals, components}); change records are appended to
    `changes`. `pod_priority` maps a PriorityClass name to the components whose pods use it. A kube-system object
    is a finding only when it was written after the application was `deployed`."""
    apps_created = [c.get("created") for c in components.values() if not c.get("related")]
    services = _service_index(components)
    ready = {
        f"{c['namespace']}/{s.get('name')}": s.get("ready_endpoints")
        for c in components.values()
        for s in c.get("services") or []
    }
    out: list[dict] = []

    for ing in kubectl_items("ingresses", None, errors):
        meta = ing.get("metadata") or {}
        ns, name = meta.get("namespace"), meta.get("name")
        linked, signals, routes = set(), [], []
        for route, svc, port in _backends(ing):
            key = f"{ns}/{svc}"
            routes.append(f"{route} -> Service {svc}:{port}")
            if key in services:
                linked.add(services[key])
                if ready.get(key) == 0:
                    signals.append(f"routes {route} to Service {svc}, which has 0 ready endpoints")
            elif ns in namespaces:
                signals.append(f"routes {route} to Service {svc}:{port}, which selects no workload or does not exist")
        if not linked and ns not in namespaces:
            continue
        out.append({**_finding("Ingress", name, ns, signals, linked), "routes": routes})
        _change(changes, "Ingress", name, ns, meta, linked, apps_created, deployed)

    for pc in kubectl_items("priorityclasses", None, errors, cluster_scoped=True):
        meta = pc.get("metadata") or {}
        name = meta.get("name")
        users = set(pod_priority.get(name) or set())
        if pc.get("globalDefault"):
            users |= set(components)
        if not users:
            continue
        settings = (
            f"value {pc.get('value')}, globalDefault {bool(pc.get('globalDefault'))}, "
            f"preemptionPolicy {pc.get('preemptionPolicy') or 'PreemptLowerPriority'}"
        )
        out.append({**_finding("PriorityClass", name, None, [], users), "settings": settings})
        _change(changes, "PriorityClass", name, None, meta, users, apps_created, deployed)

    for resource, kind in (("daemonsets", "DaemonSet"), ("cronjobs", "CronJob")):
        for obj in kubectl_items(resource, "kube-system", errors):
            meta = obj.get("metadata") or {}
            if _change(changes, kind, meta.get("name"), "kube-system", meta, set(), apps_created, deployed):
                out.append(_finding(kind, meta.get("name"), "kube-system", [f"kube-system {kind} written"], set()))
    return out


def _finding(kind: str, name: str, ns: str | None, signals: list[str], comps: set[str]) -> dict:
    return {"kind": kind, "name": name, "namespace": ns, "signals": signals, "components": sorted(comps)}


def _change(
    changes: list[dict],
    kind: str,
    name: str,
    ns: str | None,
    meta: dict,
    affects: set[str],
    apps_created: list,
    deployed: datetime | None,
) -> bool:
    """Record the object's change, if any; True when it was made after the application was deployed."""
    obj_id = f"{kind.lower()}/{ns}/{name}" if ns else f"{kind.lower()}/{name}"
    change = change_log.object_change(
        obj_id, kind, name, ns, meta, affects=sorted(affects), targets_created=apps_created
    )
    if change:
        changes.append(change)
    return change is not None and not change_log.before_deploy(change, deployed)
