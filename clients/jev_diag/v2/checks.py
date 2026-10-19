"""Deterministic cross-object checks: facts code can establish by comparing Kubernetes objects.

Every function here returns or attaches observations for the model to weigh. None of them decides the root
cause, and none depends on when the application was deployed or on what a particular fault looks like:

* the live pod against the pod template it was created from, and the mutating webhooks that intercept it;
* a Service with internalTrafficPolicy=Local against the nodes its clients run on;
* how a workload reads each ConfigMap or Secret (env values are fixed at container start, mounted files are
  updated in place, subPath mounts are not);
* objects stuck in Terminating on a finalizer, and ReadWriteOnce claims shared by several replicas.
"""

from __future__ import annotations

import json
import re

from clients.jev_diag.timeutil import seconds_ago

# --------------------------------------------------------------------------- quantities


def parse_cpu_millis(quantity: str | None) -> float | None:
    if not quantity:
        return None
    q = str(quantity)
    try:
        if q.endswith("m"):
            return float(q[:-1])
        if q.endswith("n"):
            return float(q[:-1]) / 1_000_000
        if q.endswith("u"):
            return float(q[:-1]) / 1_000
        return float(q) * 1000
    except ValueError:
        return None


_MEM_UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def parse_memory_bytes(quantity: str | None) -> float | None:
    if not quantity:
        return None
    q = str(quantity)
    for suffix, mult in sorted(_MEM_UNITS.items(), key=lambda kv: -len(kv[0])):
        if q.endswith(suffix):
            try:
                return float(q[: -len(suffix)]) * mult
            except ValueError:
                return None
    try:
        return float(q)
    except ValueError:
        return None


def same_quantity(resource: str, a: str | None, b: str | None) -> bool:
    """Compare two resource quantities by value ("1" and "1000m" are the same CPU)."""
    if a == b:
        return True
    if a is None or b is None:
        return False
    parse = parse_cpu_millis if resource == "cpu" else parse_memory_bytes
    x, y = parse(a), parse(b)
    return x is not None and x == y


# --------------------------------------------------------------------------- selectors and rules


def label_selector_matches(selector: dict | None, labels: dict | None) -> bool:
    """Kubernetes LabelSelector semantics (matchLabels and matchExpressions); an empty selector matches everything."""
    if not selector:
        return True
    labels = labels or {}
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def rules_cover_pod_create(rules: list[dict] | None) -> bool:
    """True when admission rules include CREATE of core-group pods."""
    for rule in rules or []:
        ops = rule.get("operations") or []
        groups = rule.get("apiGroups") or []
        resources = rule.get("resources") or []
        if (
            ("CREATE" in ops or "*" in ops)
            and ("" in groups or "*" in groups)
            and any(r in ("pods", "*", "*/*") for r in resources)
        ):
            return True
    return False


# --------------------------------------------------------------------------- probes


def describe_probe(probe: dict | None) -> str:
    """One-line description of a probe handler with every target field (scheme, path, port, command)."""
    if not probe:
        return "none"
    if "httpGet" in probe:
        g = probe["httpGet"]
        return f"httpGet {(g.get('scheme') or 'HTTP').upper()} :{g.get('port')}{g.get('path') or '/'}"
    if "tcpSocket" in probe:
        return f"tcpSocket :{probe['tcpSocket'].get('port')}"
    if "grpc" in probe:
        return f"grpc :{probe['grpc'].get('port')}"
    if "exec" in probe:
        return "exec " + " ".join(map(str, probe["exec"].get("command") or []))[:100]
    return "other"


# --------------------------------------------------------------------------- env addresses


_URL_HOST_RE = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://(?:[^@/\s]*@)?(?P<host>[^:/?#\s]+)")
_HOSTPORT_RE = re.compile(
    r"(?i)(?<![\w.@-])(?P<host>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*):(?P<port>\d{2,5})(?![\w.-])"
)
_BARE_HOST_RE = re.compile(r"(?i)^[a-z](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*$")


def env_host_candidates(value: str) -> set[str]:
    """Host names an env value could address: URL hosts, host:port pairs, and a bare host name."""
    out: set[str] = set()
    text = (value or "").strip()
    if not text or "$(" in text:
        return out
    for part in re.split(r"[,;\s]+", text):
        m = _URL_HOST_RE.match(part)
        if m:
            out.add(m.group("host").lower())
            continue
        found = [h.group("host").lower() for h in _HOSTPORT_RE.finditer(part)]
        out.update(found)
        if not found and _BARE_HOST_RE.match(part):
            out.add(part.lower())
    return out


def service_host_index(components: dict[str, dict]) -> dict[str, tuple[str, str]]:
    """Every in-cluster name of every Service the application exposes -> ("ns/service", owner component id)."""
    index: dict[str, tuple[str, str]] = {}
    for cid, comp in components.items():
        ns = comp["namespace"]
        for svc in comp.get("services") or []:
            name = svc.get("name")
            if not name:
                continue
            for host in (name, f"{name}.{ns}", f"{name}.{ns}.svc", f"{name}.{ns}.svc.cluster.local"):
                index.setdefault(host.lower(), (f"{ns}/{name}", cid))
    return index


def resolve_env_hosts(components: dict[str, dict]) -> None:
    """Record which Services each workload addresses through its env values, and each workload's clients.

    Sets `calls_services` (["ns/service"]) and `_calls` (owner component ids) on callers, and `clients`
    (component ids) on the owners of the Services they address. A bare host counts only in the caller's own
    namespace, where Kubernetes DNS resolves it.
    """
    index = service_host_index(components)
    clients: dict[str, set[str]] = {}
    for cid, comp in components.items():
        services, owners = set(), set()
        for host in comp.get("_env_hosts") or []:
            hit = index.get(host)
            if hit is None:
                continue
            svc, owner = hit
            if "." not in host and not svc.startswith(comp["namespace"] + "/"):
                continue
            if owner == cid:
                continue
            services.add(svc)
            owners.add(owner)
            clients.setdefault(owner, set()).add(cid)
        if services:
            comp["calls_services"] = sorted(services)
            comp["_calls"] = sorted(owners)
    for owner, callers in clients.items():
        components[owner]["clients"] = sorted(callers)


# --------------------------------------------------------------------------- config read modes


def config_ref_modes(pod_spec: dict) -> dict[str, list[str]]:
    """How the pod reads each ConfigMap and Secret: "env NAME", "envFrom", "volume PATH", or "subPath PATH"."""
    modes: dict[str, list[str]] = {}

    def add(kind: str, name: str | None, mode: str) -> None:
        if name:
            modes.setdefault(f"{kind}/{name}", [])
            if mode not in modes[f"{kind}/{name}"]:
                modes[f"{kind}/{name}"].append(mode)

    volumes: dict[str, list[tuple[str, str]]] = {}
    for v in pod_spec.get("volumes") or []:
        refs: list[tuple[str, str]] = []
        if v.get("configMap"):
            refs.append(("ConfigMap", v["configMap"].get("name")))
        if v.get("secret"):
            refs.append(("Secret", v["secret"].get("secretName")))
        for src in (v.get("projected") or {}).get("sources") or []:
            if src.get("configMap"):
                refs.append(("ConfigMap", src["configMap"].get("name")))
            if src.get("secret"):
                refs.append(("Secret", src["secret"].get("name")))
        volumes[v.get("name")] = refs
    for c in (pod_spec.get("initContainers") or []) + (pod_spec.get("containers") or []):
        for e in c.get("env") or []:
            vf = e.get("valueFrom") or {}
            if vf.get("secretKeyRef"):
                add("Secret", vf["secretKeyRef"].get("name"), f"env {e.get('name')}")
            if vf.get("configMapKeyRef"):
                add("ConfigMap", vf["configMapKeyRef"].get("name"), f"env {e.get('name')}")
        for ref in c.get("envFrom") or []:
            if ref.get("configMapRef"):
                add("ConfigMap", ref["configMapRef"].get("name"), "envFrom")
            if ref.get("secretRef"):
                add("Secret", ref["secretRef"].get("name"), "envFrom")
        for m in c.get("volumeMounts") or []:
            for kind, name in volumes.get(m.get("name"), []):
                add(kind, name, f"{'subPath' if m.get('subPath') else 'volume'} {m.get('mountPath')}")
    return modes


# --------------------------------------------------------------------------- pod vs template (admission)


def container_spec_summary(pod_spec: dict) -> dict[str, dict]:
    """The fields admission commonly rewrites, per container (init containers included)."""
    out = {}
    for c in (pod_spec.get("initContainers") or []) + (pod_spec.get("containers") or []):
        res = c.get("resources") or {}
        out[c.get("name")] = {
            "image": c.get("image"),
            "limits": res.get("limits") or {},
            "requests": res.get("requests") or {},
            "env": sorted({e.get("name") for e in c.get("env") or [] if e.get("name")}),
        }
    return out


def _limitrange_defaults(limit_ranges: list[dict], namespace: str) -> dict[tuple[str, str], tuple[str, str]]:
    """(field, resource) -> (value, LimitRange name) for Container defaults in the namespace."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for lr in limit_ranges:
        if lr.get("kind") != "LimitRange" or lr.get("namespace") != namespace:
            continue
        for item in lr.get("limits") or []:
            if item.get("type") not in (None, "Container"):
                continue
            for res, value in (item.get("default") or {}).items():
                out[("limits", res)] = (value, lr.get("name"))
            for res, value in (item.get("defaultRequest") or {}).items():
                out[("requests", res)] = (value, lr.get("name"))
    return out


def container_differences(
    template: dict[str, dict], pod: dict[str, dict], defaults: dict[tuple[str, str], tuple[str, str]]
) -> list[str]:
    """Differences between the template's containers and the pod's, except values Kubernetes fills in itself.

    Two documented defaults are not differences: a request the template leaves unset takes the container's limit,
    and a LimitRange's default and defaultRequest fill values the template leaves unset.
    """
    diffs: list[str] = []
    for name, pc in pod.items():
        tc = template.get(name)
        if tc is None:
            diffs.append(f"container {name} exists in the pod but not in the template")
            continue
        if pc["image"] != tc["image"]:
            diffs.append(f"container {name} image {pc['image']}, template {tc['image']}")
        for part in ("limits", "requests"):
            for res in sorted(set(pc[part]) | set(tc[part])):
                have, want = pc[part].get(res), tc[part].get(res)
                if same_quantity(res, have, want):
                    continue
                if part == "requests" and want is None and same_quantity(res, have, tc["limits"].get(res)):
                    continue  # Kubernetes sets a missing request to the container's limit when it creates the pod
                default = defaults.get((part, res))
                if want is None and default and same_quantity(res, have, default[0]):
                    continue  # the LimitRange default, applied as documented
                diffs.append(f"container {name} {part}.{res} {have or 'unset'}, template {want or 'unset'}")
        added = sorted(set(pc["env"]) - set(tc["env"]))
        if added:
            diffs.append(f"container {name} env adds {', '.join(added[:5])}")
    for name in template:
        if name not in pod:
            diffs.append(f"container {name} of the template is missing from the pod")
    return diffs


def template_for_pod(pod: dict, comp: dict, revisions: dict) -> dict | None:
    """The pod template this pod was created from: its ReplicaSet or ControllerRevision, else a Job's template."""
    revs = revisions.get((comp["namespace"], comp["kind"], comp["name"])) or []
    owners = {name for _kind, name in pod.get("_owner") or []}
    rev_hash = (pod.get("_labels") or {}).get("controller-revision-hash")
    for rev in revs:
        if rev["name"] in owners:
            return rev["template"]
        if rev_hash and (rev["name"] == rev_hash or rev.get("hash") == rev_hash):
            return rev["template"]
    if comp["kind"] in ("CronJob", "Job"):
        return comp.get("_template")
    return None


def admission_changes(
    components: dict[str, dict],
    revisions: dict,
    webhooks: list[dict],
    namespace_labels: dict[str, dict],
    limit_ranges: list[dict],
) -> None:
    """Compare each running pod with the template it was created from; name the mutating webhooks that match it.

    A difference the template cannot explain was made at admission. The candidates are the mutating webhooks
    whose rules cover pod CREATE, whose namespaceSelector matches the pod's namespace, whose objectSelector
    matches the pod's labels, and whose backend has ready endpoints (a webhook that cannot be reached cannot
    have rewritten the pod). A container injected into the pods of most workloads is cluster-standard
    (a service-mesh sidecar, for example) and is not reported per workload.
    """
    found: dict[str, tuple[dict, list[str]]] = {}
    injected_counts: dict[str, int] = {}
    for cid, comp in components.items():
        defaults = _limitrange_defaults(limit_ranges, comp["namespace"])
        for pod in comp.get("pods") or []:
            spec = pod.get("_spec")
            template = template_for_pod(pod, comp, revisions) if spec else None
            if not template:
                continue
            diffs = container_differences(container_spec_summary(template.get("spec") or {}), spec, defaults)
            if diffs:
                found.setdefault(cid, (pod, diffs))
                for d in diffs:
                    if d.endswith("exists in the pod but not in the template"):
                        injected_counts[d] = injected_counts.get(d, 0) + 1
                break
    standard = {d for d, n in injected_counts.items() if n > max(1, len(components) // 2)}
    for cid, (pod, diffs) in found.items():
        diffs = [d for d in diffs if d not in standard]
        if not diffs:
            continue
        comp = components[cid]
        labels = pod.get("_labels") or {}
        ns_labels = namespace_labels.get(comp["namespace"]) or {"kubernetes.io/metadata.name": comp["namespace"]}
        matching = []
        for wh in webhooks:
            if wh.get("kind") != "MutatingWebhookConfiguration" or not rules_cover_pod_create(wh.get("_rules")):
                continue
            if not label_selector_matches(wh.get("namespace_selector"), ns_labels):
                continue
            if not label_selector_matches(wh.get("object_selector"), labels):
                continue
            if wh.get("backend_ready_endpoints") == 0:
                continue
            matching.append(wh)
        names = sorted({wh["configuration"] for wh in matching})
        tail = (
            f"; mutating webhooks whose rules and selectors match these pods: {', '.join(names)}"
            if names
            else "; no mutating webhook's rules and selectors match these pods"
        )
        comp["signals"].append(
            f"pod {pod['name']}: {'; '.join(diffs[:3])} — the running pod differs from the pod template of its "
            f"revision, so it was changed at admission{tail}"
        )
        comp["admission_changes"] = {"pod": pod["name"], "differences": diffs[:5], "webhooks": names}
        for wh in matching:
            wh.setdefault("components", [])
            if cid not in wh["components"]:
                wh["components"].append(cid)
            wh["signals"].append(f"rewrites pods of {cid} at admission ({diffs[0]})")


# --------------------------------------------------------------------------- traffic policy


def short_node(name: str | None) -> str:
    return (name or "?").split(".")[0]


def local_traffic_policy_gaps(components: dict[str, dict]) -> None:
    """For a Service with internalTrafficPolicy=Local, find clients running on nodes that have no ready endpoint.

    Clients are the workloads whose env values address the Service (see resolve_env_hosts). kube-proxy sends a
    client's traffic only to endpoints on the client's own node, so a client pod on a node without one gets no
    endpoint at all.
    """
    clients_of: dict[str, list[str]] = {}
    for cid, comp in components.items():
        for svc in comp.get("calls_services") or []:
            clients_of.setdefault(svc, []).append(cid)
    for owner in components.values():
        for svc in owner.get("services") or []:
            if svc.get("internal_traffic_policy") != "Local":
                continue
            endpoint_nodes = set(svc.get("endpoint_nodes") or [])
            gaps = []
            for client in clients_of.get(f"{owner['namespace']}/{svc['name']}", []):
                nodes = sorted(
                    {
                        p.get("node")
                        for p in components[client].get("pods") or []
                        if p.get("node") and p.get("phase") == "Running"
                    }
                )
                missing = [n for n in nodes if n not in endpoint_nodes]
                if missing:
                    gaps.append((client, missing))
            if not gaps:
                continue
            where = ", ".join(short_node(n) for n in sorted(endpoint_nodes)) or "no node"
            svc["local_policy_gaps"] = [f"{c} on {', '.join(short_node(n) for n in m)}" for c, m in gaps]
            owner["signals"].append(
                f"service {svc['name']} (internalTrafficPolicy=Local) has ready endpoints only on {where}; "
                + "; ".join(
                    f"client {c} runs on {', '.join(short_node(n) for n in m)}, where calls to {svc['name']} find "
                    "no local endpoint"
                    for c, m in gaps
                )
            )


# --------------------------------------------------------------------------- stuck objects and volumes

_RESOURCE_PLURALS = {
    "ConfigMap": "configmaps",
    "Secret": "secrets",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "Service": "services",
    "Job": "jobs",
    "Deployment": "deployments",
    "StatefulSet": "statefulsets",
}


def _granted_verbs(rules: list[str], resource: str) -> set[str] | None:
    """Verbs a role's rule lines ("configmaps [core]: get,list,watch") grant on `resource`; None if no rule names it."""
    verbs: set[str] | None = None
    for rule in rules:
        head, _, tail = rule.partition(":")
        resources = head.split(" ")[0].split(",")
        if resource in resources or "*" in resources:
            verbs = (verbs or set()) | {v.strip() for v in tail.split(",") if v.strip()}
    return verbs


def stuck_terminating(objects: list[tuple[str, dict]]) -> list[dict]:
    """Objects whose deletion is waiting on finalizers that nobody has removed."""
    out = []
    for kind, obj in objects:
        meta = obj.get("metadata") or {}
        if not meta.get("deletionTimestamp") or not meta.get("finalizers"):
            continue
        out.append(
            {
                "kind": kind,
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "finalizers": list(meta["finalizers"])[:4],
                "terminating_for_s": seconds_ago(meta["deletionTimestamp"]),
                "components": [],
            }
        )
    return out[:10]


def link_stuck_objects(stuck: list[dict], components: dict[str, dict], rbac: list[dict]) -> None:
    """Link each stuck object to the workload expected to clear its finalizer.

    First choice: a workload whose logs show authorization denials and whose bound role covers the object's
    resource type (the controller is trying and being refused). Otherwise: a workload whose full name appears
    in a finalizer name.
    """
    for obj in stuck:
        resource = _RESOURCE_PLURALS.get(obj["kind"], obj["kind"].lower() + "s")
        linked: list[tuple[str, str]] = []
        for entry in rbac:
            verbs = _granted_verbs(entry.get("rules") or [], resource)
            if verbs is None:
                continue
            for cid in entry.get("components") or []:
                if any(
                    s.startswith(f"bound workload {cid} logs authorization denials") for s in entry.get("signals") or []
                ):
                    linked.append(
                        (
                            cid,
                            f"its {entry['kind']} {entry['name']} covers {resource} and its logs show authorization denials",
                        )
                    )
                    if not verbs & {"patch", "update", "*"}:
                        obj["permission_gap"] = (
                            f"removing a finalizer requires patch or update on {resource}, which {entry['kind']} "
                            f"{entry['name']} does not grant (it grants {', '.join(sorted(verbs)) or 'nothing'})"
                        )
        if not linked:
            for cid, comp in components.items():
                if any(comp["name"].lower() in f.lower() for f in obj["finalizers"]):
                    linked.append((cid, "a finalizer on it names this component"))
        seen = set()
        for cid, why in linked:
            if cid in seen or cid not in components:
                continue
            seen.add(cid)
            obj["components"].append(cid)
            gap = f"; {obj['permission_gap']}" if obj.get("permission_gap") else ""
            components[cid]["signals"].append(
                f"{obj['kind']} {obj['name']} has been terminating for {obj['terminating_for_s']}s, held by finalizer "
                f"{', '.join(obj['finalizers'])}; linked to this component because {why}{gap}"
            )


def shared_rwo_claims(components: dict[str, dict], access_modes: dict[tuple[str, str], list[str]]) -> None:
    """A ReadWriteOnce claim can attach to one node at a time; several Deployment replicas cannot all mount it."""
    for comp in components.values():
        if comp.get("kind") != "Deployment":
            continue
        desired = (comp.get("replicas") or {}).get("desired") or 0
        if desired < 2:
            continue
        for vol in comp.get("volumes") or []:
            claim = vol.get("pvc")
            modes = access_modes.get((comp["namespace"], claim)) if claim else None
            if not modes or not set(modes) <= {"ReadWriteOnce", "ReadWriteOncePod"}:
                continue
            spread = (
                " and its required pod anti-affinity places replicas on different nodes"
                if comp.get("anti_affinity_required")
                else ""
            )
            comp["signals"].append(
                f"PVC {claim} is {'/'.join(modes)} (attachable to one node at a time) but {desired} replicas of this "
                f"Deployment mount it{spread}; replicas scheduled on another node cannot attach it"
            )


def json_short(value, limit: int = 120) -> str:
    text = json.dumps(value, sort_keys=True, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"
