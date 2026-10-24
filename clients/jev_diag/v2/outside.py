"""Objects outside the application's namespaces that can carry its fault, and namespaces the application reaches
through its own configuration.

* Ingresses whose backend is an application Service (missing backend, no ready endpoints, or a recent write).
* PriorityClasses used by the application's pods, or set as the cluster default.
* Namespaces named in the application's own addresses (`name.other-ns.svc`), read like the application's own.
* Any other object, of any kind and anywhere in the cluster, that an operator tool (kubectl, a client library) wrote
  after the application was deployed: the cluster's change log, read from each object's write history. Writes by
  Kubernetes' own components and by an object's own controller are left out, and so are namespaces the run is
  configured to ignore. Writes the cluster's own records attribute to a controller are kept but marked routine
  (`routine_write`, `bulk_bookkeeping`): they are ranked last and summarized, not listed one by one.

Each object is listed with the components it acts on. Only an abnormal signal (a missing backend, no ready
endpoints) or a write made after the application was deployed makes it a cluster finding; its routes or settings
are context only. Earlier writes are dropped when changes are ranked.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime

from clients.jev_diag.timeutil import parse_time
from clients.jev_diag.v2 import changes as change_log
from clients.jev_diag.v2.checks import label_selector_matches
from clients.jev_diag.v2.collector import kubectl_items, run_kubectl
from clients.jev_diag.v2.derive import OBSERVABILITY_RE, failed_reads

# Kubernetes' own namespaces, and any the run is configured to leave out (JEV_DIAG_IGNORE_NAMESPACES, comma-separated:
# tooling of the environment that is not part of the system being diagnosed).
CONFIGURED_IGNORES = {ns.strip() for ns in os.environ.get("JEV_DIAG_IGNORE_NAMESPACES", "").split(",") if ns.strip()}
IGNORED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"} | CONFIGURED_IGNORES
# Resources controllers rewrite constantly.
_CHURN_RESOURCES = {
    "events",
    "events.events.k8s.io",
    "pods",
    "endpoints",
    "endpointslices.discovery.k8s.io",
    "leases.coordination.k8s.io",
    "replicasets.apps",
    "controllerrevisions.apps",
    "componentstatuses",
    "podtemplates",
}
# Workload kinds: the application's own workloads are read and dated by the per-namespace collection (template
# revisions included). Every other kind in an application namespace goes through this log, which skips any object
# another collection already dated (see `known` in change_radar).
_READ_IN_APP = {
    "deployments.apps",
    "statefulsets.apps",
    "daemonsets.apps",
    "cronjobs.batch",
    "jobs.batch",
}
# The Kubernetes CLI and the package manager record a person's (or a person's script's) intent; their writes are never
# bookkeeping, however many objects one command writes.
_INTENT_TOOLS = re.compile(r"^(?:kubectl|helm)(?:-|$)")
BULK_OBJECTS = 3  # a background writer writing this many unreferenced objects of one kind is doing bookkeeping
_PROVISIONED_BY = "pv.kubernetes.io/provisioned-by"  # set by a volume provisioner on the volume it created for a claim
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
    return sorted(found - set(namespaces) - IGNORED_NAMESPACES)


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
    *,
    cert_problems: dict[tuple[str, str], list[str]] | None = None,
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
    unread = failed_reads(errors)

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
            elif ns in namespaces and unread & {("services", ns), ("services", None)}:
                signals.append(f"routes {route} to Service {svc}:{port}, which could not be read (not verified)")
            elif ns in namespaces:
                signals.append(f"routes {route} to Service {svc}:{port}, which selects no workload or does not exist")
        for tls in (ing.get("spec") or {}).get("tls") or []:
            for problem in (cert_problems or {}).get((ns, tls.get("secretName")), []):
                signals.append(f"serves TLS with Secret {tls.get('secretName')}: {problem}")
        if not linked and ns not in namespaces:
            continue
        out.append({**_finding("Ingress", name, ns, signals, linked), "routes": routes})
        _change(changes, "Ingress", name, ns, meta, linked, apps_created, deployed, ing)

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
        _change(changes, "PriorityClass", name, None, meta, users, apps_created, deployed, pc)

    for (ns, name), problems in (cert_problems or {}).items():  # a certificate that cannot be served is a finding
        users = {
            c
            for c, comp in components.items()
            if comp["namespace"] == ns and f"secret/{name}" in (comp.get("config_objects") or {})
        }
        out.append(_finding("Secret", name, ns, [f"TLS Secret {name}: {p}" for p in problems], users))

    return out


def change_radar(
    namespaces: list[str],
    components: dict[str, dict],
    errors: list[str],
    changes: list[dict],
    deployed: datetime | None,
) -> list[dict]:
    """Objects anywhere in the cluster that an operator tool wrote after the application was deployed, as findings
    with change records appended to `changes`. Only the operator's own writes count (a controller updating the same
    object later does not make it a change), and objects another collection already dates are skipped. A Node acts
    on the components its taints or cordon keep from scheduling (`kept_off`), and on those with pods on it only when
    a taint evicts them (NoExecute).

    Routine writes (`routine_write`, `bulk_bookkeeping`) keep their marked change record but are no finding of
    their own. Telemetry ownership is a role tag, not a reason to hide an operator's write."""
    if deployed is None:
        return []
    try:
        resources = run_kubectl(["api-resources", "--verbs=list", "-o", "name"]).split()
    except Exception as exc:  # noqa: BLE001 - no discovery: no change log
        errors.append(f"api-resources: {exc}")
        return []
    apps_created = [c.get("created") for c in components.values() if not c.get("related")]
    known = {c["object"] for c in changes}
    # Faults come after the application finished deploying: an object written while it was still being deployed (a
    # ConfigMap applied minutes after the first workload) is part of the deploy, not a change.
    settled = max((t for t in (parse_time(c) for c in apps_created) if t), default=deployed)
    on_node: dict[str, set[str]] = {}
    for cid, comp in components.items():
        for pod in comp.get("pods") or []:
            on_node.setdefault(pod.get("node") or "", set()).add(cid)
    background: set[tuple[str, str]] = set()  # (writer, kind) seen writing that kind before the application existed
    written: list[tuple[dict, dict]] = []  # (finding, change record) of each object written after the deploy
    first_record = len(changes)
    for resource in resources:
        if resource in _CHURN_RESOURCES:
            continue
        for obj in kubectl_items(resource, None, errors):
            meta = obj.get("metadata") or {}
            ns, name = meta.get("namespace"), meta.get("name") or ""
            if ns in CONFIGURED_IGNORES:
                continue
            if ns in namespaces and resource in _READ_IN_APP:
                continue
            writes = change_log.desired_state_writes(meta)
            if not writes:
                continue
            kind = obj.get("kind") or resource
            background |= _wrote_before(kind, meta, writes, deployed)
            if (f"{kind.lower()}/{ns}/{name}" if ns else f"{kind.lower()}/{name}") in known:
                continue  # read and dated by its own collection
            if kind == "Node":
                # a node's spec decides where new pods go; only a NoExecute taint reaches the pods already on it
                taints = (obj.get("spec") or {}).get("taints") or []
                evicts = any(t.get("effect") == "NoExecute" for t in taints)
                affects = kept_off(obj, components) | (on_node.get(name, set()) if evicts else set())
            else:
                affects = acting_on(kind, name, ns, obj, components)
            marks = {"routine": routine_write(kind, obj, writes)}
            marks["role"] = "observability" if _telemetry(name, affects, components) else None
            meta = {**meta, "managedFields": writes}
            change = _change(changes, kind, name, ns, meta, affects, apps_created, settled, obj, marks)
            if change:
                after = [w for w in writes if (parse_time(w.get("time")) or settled) >= settled]
                managers = ", ".join(dict.fromkeys(w["manager"] for w in after or writes))
                finding = _finding(
                    kind, name, ns, [f"written by {managers} after the application was deployed"], affects
                )
                written.append((finding, change))
    bulk_bookkeeping(changes[first_record:], background)
    return [f for f, change in written if not change.get("routine")]


def routine_write(kind: str, obj: dict, writes: list[dict]) -> str | None:
    """Why an object's writes are a controller doing its job, from the object's own records, or None. Only an object
    nobody else wrote qualifies: a person's later edit makes it a change again.

    * A PersistentVolume its provisioner created for a claim (`pv.kubernetes.io/provisioned-by`, `spec.claimRef`):
      dynamic provisioning answering a PersistentVolumeClaim.
    * An object with a controlling owner (`ownerReferences[].controller`): its owner's controller maintains it.
    """
    if len({w.get("manager") for w in writes}) != 1:
        return None
    meta = obj.get("metadata") or {}
    if kind == "PersistentVolume" and _PROVISIONED_BY in (meta.get("annotations") or {}):
        if (obj.get("spec") or {}).get("claimRef"):
            return "volume provisioned for a claim"
    if any(o.get("controller") for o in meta.get("ownerReferences") or []):
        return "kept by its owner's controller"
    return None


def _wrote_before(kind: str, meta: dict, writes: list[dict], deployed: datetime) -> set[tuple[str, str]]:
    """(writer, kind) for each writer this object shows writing objects of its kind before the application was
    deployed: a write dated before it, or the object's only writer having created it before."""
    out = {(w.get("manager"), kind) for w in writes if (t := parse_time(w.get("time"))) and t < deployed}
    created = parse_time(meta.get("creationTimestamp"))
    managers = {w.get("manager") for w in writes}
    if len(managers) == 1 and created and created < deployed:
        out.add((managers.pop(), kind))
    return out


def bulk_bookkeeping(changes: list[dict], background: set[tuple[str, str]]) -> None:
    """Mark as routine a background writer's bulk writes: objects that act on no component, of kinds their writer
    already wrote before the application existed, when that writer wrote at least BULK_OBJECTS of them. That is a
    controller keeping its own records as pods come and go (address allocations, for example), not a change to what
    the application runs. Writes recorded by the Kubernetes CLI or the package manager express intent and never
    qualify; neither does a writer new to the kind (a script creating several objects at once) or a single write of a
    kind the writer had written before (a script that set up the cluster changing one of its objects)."""

    def candidate(change: dict) -> bool:
        writer = change.get("writer") or ""
        return (
            not change["affects"]
            and not change.get("routine")
            and (writer, change["kind"]) in background
            and not _INTENT_TOOLS.match(writer)
        )

    counts = Counter(c["writer"] for c in changes if candidate(c))
    for change in changes:
        if candidate(change) and counts[change["writer"]] >= BULK_OBJECTS:
            change["routine"] = "bulk bookkeeping by a writer that kept this kind before the application"


def _telemetry(name: str, affects: set[str], components: dict[str, dict]) -> bool:
    """A telemetry object, named as telemetry components are recognised: every component it acts on is one, or it
    acts on none and carries a telemetry product's name itself."""
    names = [components[c]["name"] for c in affects if c in components] or ([] if affects else [name])
    return bool(names) and all(OBSERVABILITY_RE.search(n) for n in names)


def kept_off(node: dict, components: dict[str, dict]) -> set[str]:
    """Components whose pods the scheduler could not place because of this node: the scheduling failures (events
    and pod conditions) name one of its taints or report cordoned nodes. A NoSchedule taint acts on those, not on
    the pods already running on the node."""
    spec = node.get("spec") or {}
    marks = [f"taint {{{t.get('key')}:" for t in spec.get("taints") or [] if t.get("key")]
    marks += ["were unschedulable"] if spec.get("unschedulable") else []
    if not marks:
        return set()
    out = set()
    for cid, comp in components.items():
        texts = [
            e.get("message") or "" for e in comp.get("warning_events") or [] if e.get("reason") == "FailedScheduling"
        ]
        texts += [
            c for pod in comp.get("pods") or [] for c in pod.get("conditions") or [] if c.startswith("PodScheduled")
        ]
        if any(m in t for m in marks for t in texts):
            out.add(cid)
    return out


# Kinds that apply to every workload of their namespace by definition.
_NAMESPACE_WIDE = {"ResourceQuota", "LimitRange"}


def acting_on(kind: str, name: str, ns: str | None, obj: dict, components: dict[str, dict]) -> set[str]:
    """The workloads an object is known to act on, from references in either direction: its selectors that match a
    workload's pod labels, its values that name a workload or one of its Services, and the workloads that mount,
    read, or run as it (for a PersistentVolume, those that mount its claim). A Namespace acts on its workloads, and
    so does a namespace policy. An object nothing references acts on no workload in particular: it is context, not a
    change of every workload around it."""
    if kind == "Namespace":
        return {c for c, comp in components.items() if comp["namespace"] == name}
    if kind == "PersistentVolume":  # a volume reaches the workloads that mount the claim bound to it
        claim = (obj.get("spec") or {}).get("claimRef") or {}
        return {
            c
            for c, comp in components.items()
            if comp["namespace"] == claim.get("namespace")
            and claim.get("name") in {v.get("pvc") for v in comp.get("volumes") or []}
        }
    in_ns = {c: comp for c, comp in components.items() if comp["namespace"] == ns}
    if kind in _NAMESPACE_WIDE:
        return set(in_ns)
    body = {k: v for k, v in obj.items() if k not in ("apiVersion", "kind", "metadata", "status")}
    selectors, values = list(_selectors(body)), set(_strings(body))
    out = set()
    for cid, comp in in_ns.items():
        names = {comp["name"], *(svc.get("name") for svc in comp.get("services") or [])}
        refs = config_names(comp) | {v.get("pvc") for v in comp.get("volumes") or []} | {comp.get("service_account")}
        labels = comp.get("template_labels") or {}
        if names & values or name in refs or any(label_selector_matches(sel, labels) for sel in selectors):
            out.add(cid)
    return out


def config_names(comp: dict) -> set[str]:
    refs = comp.get("config_refs") or {}
    return set(refs.get("configmaps") or []) | set(refs.get("secrets") or [])


def _selectors(node: object):
    """Label selectors anywhere in an object's body, as LabelSelectors (a Service's plain map becomes matchLabels)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("selector", "podSelector") and isinstance(value, dict) and value:
                yield value if {"matchLabels", "matchExpressions"} & set(value) else {"matchLabels": value}
            else:
                yield from _selectors(value)
    elif isinstance(node, list):
        for item in node:
            yield from _selectors(item)


def _strings(node: object):
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)
    elif isinstance(node, str):
        yield node


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
    obj: dict | None = None,
    extra: dict | None = None,
) -> dict | None:
    """Record the object's change, if any (with what it set, given the object); the record when it was made after
    the application was deployed."""
    obj_id = f"{kind.lower()}/{ns}/{name}" if ns else f"{kind.lower()}/{name}"
    change = change_log.object_change(
        obj_id, kind, name, ns, meta, affects=sorted(affects), targets_created=apps_created, obj=obj, extra=extra
    )
    if change:
        changes.append(change)
    return change if change is not None and not change_log.before_deploy(change, deployed) else None
