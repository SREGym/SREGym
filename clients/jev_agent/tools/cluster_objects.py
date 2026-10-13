"""cluster_objects: one group of cluster-wide objects (admission, quota, network, rbac, dns, nodes, storage)."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable

from clients.jev_agent.evidence import write_note
from clients.jev_agent.tools import _common
from clients.jev_agent.tools._common import MESSAGE_CHARS, _components, _namespaces, _owner_of, _when
from clients.jev_agent.types import CLUSTER_GROUPS, AgentContext, ToolResult
from clients.jev_diag import changes as change_log
from clients.jev_diag import collector
from clients.jev_diag.checks import json_short, label_selector_matches


def _finish(lines: list[str], empty: str, errors: list[str]) -> list[str]:
    """Group lines, the empty-result sentence, or the read failure (an unreadable list is not an empty one)."""
    if not lines:
        return [f"could not read: {errors[0]}"] if errors else [empty]
    return lines + ([f"{len(errors)} read error(s), first: {errors[0]}"] if errors else [])


def _rules_take_pods(rules: list[dict] | None) -> bool:
    """Whether webhook rules intercept pods (core or any API group; pods, their subresources, or any resource)."""
    return any(
        {"", "*"} & set(r.get("apiGroups") or [""]) and {"pods", "pods/*", "*", "*/*"} & set(r.get("resources") or [])
        for r in rules or []
    )


def _webhook_reach(ctx: AgentContext, wh: dict, ns_labels: dict[str, dict], pods: dict[str, list[dict]]) -> str:
    """Which of the application's pods a webhook intercepts, from its rules and selectors (as a NetworkPolicy line
    says which pods the policy selects)."""
    namespaces = [ns for ns in pods if label_selector_matches(wh.get("namespace_selector"), ns_labels.get(ns))]
    if not namespaces:
        return "intercepts nothing in the application's namespaces"
    if not _rules_take_pods(wh.get("_rules")):
        return "intercepts no pods"
    comps = _components(ctx)
    owners = {
        ns: {p["metadata"]["name"]: _owner_of(comps, p) or p["metadata"]["name"] for p in pods[ns]} for ns in pods
    }
    every = {o for ns in owners for o in owners[ns].values()}
    taken = sorted(
        {
            owners[ns][p["metadata"]["name"]]
            for ns in namespaces
            for p in pods[ns]
            if label_selector_matches(wh.get("object_selector"), (p.get("metadata") or {}).get("labels"))
        }
    )
    if not taken:
        return "intercepts none of the application's pods"
    if len(taken) == len(every):
        return f"intercepts the pods of every workload ({len(every)})"
    more = f" and {len(taken) - 4} more" if len(taken) > 4 else ""
    return f"intercepts the pods of {', '.join(taken[:4])}{more} ({len(taken)} of {len(every)} workloads)"


def _admission(ctx: AgentContext) -> list[str]:
    """One line per mutating or validating webhook configuration: the application pods it intercepts, its rules,
    selectors, failure policy, timeout and backend readiness."""
    errors: list[str] = []
    configs = collector.kubectl_items("mutatingwebhookconfigurations", None, errors, cluster_scoped=True) + (
        collector.kubectl_items("validatingwebhookconfigurations", None, errors, cluster_scoped=True)
    )
    ns_labels: dict[str, dict] = {}
    pods: dict[str, list[dict]] = {}
    for ns in _namespaces(ctx):
        with contextlib.suppress(Exception):  # unreadable: Kubernetes labels every namespace with its name
            ns_labels[ns] = (_common._get_json(["get", "namespace", ns, "-o", "json"]).get("metadata") or {}).get(
                "labels"
            )
        ns_labels[ns] = ns_labels.get(ns) or {"kubernetes.io/metadata.name": ns}
        try:
            pods[ns] = _common._ns_pods(ns)
        except Exception as exc:  # noqa: BLE001 - the webhooks are still worth listing
            errors.append(f"pods of {ns}: {exc}")
    lines = []
    for wh in collector.summarize_webhooks(configs, errors):
        backend = wh.get("backend") or {}
        target = (
            f"{backend.get('namespace')}/{backend.get('service')}:{backend.get('port')}"
            if backend.get("service")
            else f"url {backend.get('url')}"
        )
        reach = _webhook_reach(ctx, wh, ns_labels, pods) if pods else "intercepts pods not known"
        # Named by its configuration, the object one fixes or deletes (its webhooks' names need not resemble it),
        # and where it acts comes first, so a shortened line still tells the webhooks apart.
        lines.append(
            f"{wh['kind']} {wh.get('configuration')} (created {_when(ctx, wh.get('created'))}): {reach} through "
            f"webhook {wh['webhook']}; rules {wh.get('rules')}; namespaceSelector "
            f"{json_short(wh.get('namespace_selector'), 60)}; objectSelector {json_short(wh.get('object_selector'), 40)}; "
            f"failurePolicy {wh.get('failure_policy')}; timeout {wh.get('timeout_seconds')}s; backend {target} with "
            f"{wh.get('backend_ready_endpoints')} ready endpoint(s)"
        )
    return _finish(lines, "no mutating or validating webhook configurations", errors)


def _quota(ctx: AgentContext) -> list[str]:
    """One line per ResourceQuota (used/hard) and LimitRange (limits) in the application's namespaces."""
    errors: list[str] = []
    lines = []
    for ns in _namespaces(ctx):
        constraints = collector.summarize_namespace_constraints(
            collector.kubectl_items("limitranges", ns, errors), collector.kubectl_items("resourcequotas", ns, errors)
        )
        for c in constraints:
            head = f"{c['kind']} {ns}/{c['name']} (created {_when(ctx, c.get('created'))})"
            if c["kind"] == "ResourceQuota":
                used = c.get("used") or {}
                lines.append(
                    head
                    + ": "
                    + ", ".join(f"{r} used {used.get(r, '?')}/{h}" for r, h in (c.get("hard") or {}).items())
                )
            else:
                lines.append(head + ": " + json_short(c.get("limits"), 180))
    return _finish(lines, f"no ResourceQuota or LimitRange in {', '.join(_namespaces(ctx))}", errors)


def _network(ctx: AgentContext) -> list[str]:
    """One line per NetworkPolicy in the application's namespaces: its pod selector, rules and the workloads it
    selects."""
    errors: list[str] = []
    comps = _components(ctx)
    lines = []
    for ns in _namespaces(ctx):
        policies = collector.kubectl_items("networkpolicies", ns, errors)
        if not policies:
            continue
        pods = _common._ns_pods(ns)
        for raw in policies:
            pol = collector.summarize_network_policy(raw)
            selector = (raw.get("spec") or {}).get("podSelector") or {}
            selected = sorted(
                {
                    _owner_of(comps, p) or p["metadata"]["name"]
                    for p in pods
                    if label_selector_matches(selector, (p.get("metadata") or {}).get("labels"))
                }
            )
            lines.append(
                f"NetworkPolicy {ns}/{pol['name']} (created {_when(ctx, raw.get('metadata', {}).get('creationTimestamp'))}): "
                f"podSelector {json_short(selector, 60)}; types {pol['policy_types']}; ingress rules "
                f"{len(pol['ingress_rules'])} {json_short(pol['ingress_rules'], 60) if pol['ingress_rules'] else ''}; "
                f"egress rules {len(pol['egress_rules'])} {json_short(pol['egress_rules'], 60) if pol['egress_rules'] else ''}; "
                f"selects {', '.join(selected[:6]) or 'no current pods'}"
            )
    return _finish(lines, f"no NetworkPolicies in {', '.join(_namespaces(ctx))}", errors)


def _rule_lines(rules: list[dict]) -> list[str]:
    """'resources [groups]: verbs' per RBAC rule."""
    out = []
    for rule in rules or []:
        resources = ",".join(rule.get("resources") or rule.get("nonResourceURLs") or ["*"])
        groups = ",".join(g or "core" for g in rule.get("apiGroups") or [])
        out.append(f"{resources}{f' [{groups}]' if groups else ''}: {','.join(rule.get('verbs') or [])}")
    return out


def _rbac(ctx: AgentContext) -> list[str]:
    """The roles bound to the workloads' service accounts with their rules, the accounts bound to nothing, and
    `kubectl auth can-i --list` for up to two non-default bound accounts."""
    errors: list[str] = []
    comps = _components(ctx)
    sas: dict[tuple[str, str], list[str]] = {}
    for cid, comp in comps.items():
        sas.setdefault((comp["namespace"], comp.get("service_account") or "default"), []).append(cid)
    bindings = [
        ("ClusterRoleBinding", b)
        for b in collector.kubectl_items("clusterrolebindings", None, errors, cluster_scoped=True)
    ]
    for ns in _namespaces(ctx):
        bindings += [("RoleBinding", b) for b in collector.kubectl_items("rolebindings", ns, errors)]
    roles: dict[tuple, dict] = {}
    lines, bound = [], set()
    for bkind, b in bindings:
        bmeta = b.get("metadata") or {}
        for subj in b.get("subjects") or []:
            key = (subj.get("namespace") or bmeta.get("namespace"), subj.get("name"))
            if subj.get("kind") != "ServiceAccount" or key not in sas:
                continue
            bound.add(key)
            ref = b.get("roleRef") or {}
            rkey = (ref.get("kind"), bmeta.get("namespace") if ref.get("kind") == "Role" else None, ref.get("name"))
            if rkey not in roles:
                args = [
                    "get",
                    (ref.get("kind") or "").lower(),
                    ref.get("name") or "",
                    "-o",
                    "json",
                    "--show-managed-fields",
                ]
                try:
                    roles[rkey] = _common._get_json(args + (["-n", rkey[1]] if rkey[1] else []))
                except Exception as exc:  # noqa: BLE001 - a missing role is a fact
                    roles[rkey] = {"error": collector.trim(str(exc), 100)}
            role = roles[rkey]
            rmeta = role.get("metadata") or {}
            rules = role.get("error") or "; ".join(_rule_lines(role.get("rules") or [])[:5]) or "no rules"
            lines.append(
                f"SA {key[0]}/{key[1]} ({len(sas[key])} workload(s)): {bkind}/{bmeta.get('name')} -> {ref.get('kind')}/"
                f"{ref.get('name')} (role last write {_when(ctx, change_log.last_spec_write(rmeta)) if rmeta else 'n/a'}): {rules}"
            )
    unbound = [f"{ns}/{sa} ({len(c)})" for (ns, sa), c in sas.items() if (ns, sa) not in bound]
    if unbound:
        lines.append("service accounts with no role binding (workloads): " + ", ".join(unbound[:6]))
    for ns, sa in sorted(k for k in bound if k[1] != "default")[:2]:
        try:
            out = collector.run_kubectl(
                ["auth", "can-i", "--list", f"--as=system:serviceaccount:{ns}:{sa}", "-n", ns], timeout=30
            )
            rows = [
                " ".join(r.split())
                for r in out.splitlines()[1:]
                if r.strip() and "selfsubject" not in r and not r.lstrip().startswith("[/")
            ]
            lines.append(f"can-i {ns}/{sa}: " + " | ".join(rows[:8]))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"can-i {ns}/{sa}: {collector.trim(str(exc), 100)}")
    return _finish(lines, "no workload service accounts found", errors)


_SERVER_BLOCK_RE = re.compile(r"^([^\s{#][^{]*?)\s*\{\s*$")


def corefile_facts(corefile: str) -> dict:
    """Server blocks (zones), forward targets, and template/rewrite/hosts blocks of a Corefile."""
    zones, depth = [], 0
    for line in corefile.splitlines():
        stripped = line.strip()
        if depth == 0:
            m = _SERVER_BLOCK_RE.match(stripped)
            if m:
                zones.append(m.group(1))
        depth += stripped.count("{") - stripped.count("}")
    forwards = [" ".join(ln.split()) for ln in corefile.splitlines() if ln.strip().startswith("forward")]
    return {"zones": zones, "forward": forwards, "blocks": collector._corefile_rules(corefile)}


def _dns(ctx: AgentContext) -> list[str]:
    """Cluster DNS: the CoreDNS Corefile (last write, server blocks, forwards, template/rewrite/hosts blocks), the
    kube-dns pods and the kube-dns endpoints."""
    lines = []
    try:
        cm = _common._get_json(
            ["get", "configmap", "coredns", "-n", "kube-system", "-o", "json", "--show-managed-fields"]
        )
        meta, corefile = cm.get("metadata") or {}, (cm.get("data") or {}).get("Corefile", "")
        facts = corefile_facts(corefile)
        after = change_log.modified_after_creation(meta)
        lines.append(
            f"ConfigMap kube-system/coredns Corefile: {len(corefile.splitlines())} lines, last write "
            f"{_when(ctx, change_log.last_spec_write(meta))}"
            + (f" ({after}s after creation)" if after else "")
            + (write_note(ctx, change_log.last_spec_write(meta)) if after else "")
            + f"; server blocks {facts['zones']}; forward {facts['forward'][:2]}"
        )
        for block in facts["blocks"][:3]:
            lines.append("ConfigMap kube-system/coredns Corefile block: " + collector.trim(block, MESSAGE_CHARS))
        if not facts["blocks"]:
            lines.append("Corefile has no template, rewrite or hosts blocks")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"coredns ConfigMap: {collector.trim(str(exc), 160)}")
    try:
        pods = _common._get_json(["get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns", "-o", "json"]).get(
            "items", []
        )
        states = []
        for p in pods:
            cs = (p.get("status") or {}).get("containerStatuses") or []
            states.append(
                f"{p['metadata']['name']} {p.get('status', {}).get('phase')} ready "
                f"{sum(1 for c in cs if c.get('ready'))}/{len(cs)} restarts {sum(c.get('restartCount', 0) for c in cs)}"
            )
        lines.append(f"kube-dns pods ({len(pods)}): " + "; ".join(states))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"kube-dns pods: {collector.trim(str(exc), 160)}")
    try:
        ep = _common._get_json(["get", "endpoints", "kube-dns", "-n", "kube-system", "-o", "json"])
        ready = sum(len(s.get("addresses") or []) for s in ep.get("subsets") or [])
        not_ready = sum(len(s.get("notReadyAddresses") or []) for s in ep.get("subsets") or [])
        lines.append(f"kube-dns Service endpoints: ready {ready}, notReady {not_ready}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"kube-dns endpoints: {collector.trim(str(exc), 160)}")
    return lines


def _nodes(ctx: AgentContext) -> list[str]:
    """One line per node: Ready and other true conditions, cordon, taints and allocatable resources."""
    errors: list[str] = []
    lines = []
    for node in collector.kubectl_items("nodes", None, errors, managed_fields=False):
        meta, spec, status = node.get("metadata") or {}, node.get("spec") or {}, node.get("status") or {}
        conds = [
            f"{c.get('type')}={c.get('status')}"
            + (f"({c.get('reason')})" if c.get("type") != "Ready" or c.get("status") != "True" else "")
            for c in status.get("conditions") or []
            if c.get("type") == "Ready" or c.get("status") == "True"
        ]
        taints = [f"{t.get('key')}={t.get('value') or ''}:{t.get('effect')}" for t in spec.get("taints") or []]
        alloc = status.get("allocatable") or {}
        lines.append(
            f"node {meta.get('name')}: {', '.join(conds)}; {'cordoned; ' if spec.get('unschedulable') else ''}"
            f"taints {taints or 'none'}; allocatable cpu {alloc.get('cpu')} mem {alloc.get('memory')} pods {alloc.get('pods')}"
        )
    return _finish(lines, "no nodes listed", errors)


def _storage(ctx: AgentContext) -> list[str]:
    """The application's PVCs (phase, class, modes, capacity, users), their PVs and any PV not Bound, and the
    StorageClasses."""
    errors: list[str] = []
    comps = _components(ctx)
    users: dict[tuple[str, str], list[str]] = {}
    for cid, comp in comps.items():
        for v in comp.get("volumes") or []:
            if v.get("pvc"):
                users.setdefault((comp["namespace"], v["pvc"]), []).append(cid)
    lines, claims = [], set()
    for ns in _namespaces(ctx):
        for pvc in collector.kubectl_items("persistentvolumeclaims", ns, errors):
            meta, spec, status = pvc.get("metadata") or {}, pvc.get("spec") or {}, pvc.get("status") or {}
            claims.add((ns, meta.get("name")))
            lines.append(
                f"PVC {ns}/{meta.get('name')}: {status.get('phase')}, class {spec.get('storageClassName')}, modes "
                f"{spec.get('accessModes')}, capacity {(status.get('capacity') or {}).get('storage')}, volume "
                f"{spec.get('volumeName')}, used by {users.get((ns, meta.get('name'))) or 'no workload'}"
                + (", terminating" if meta.get("deletionTimestamp") else "")
            )
    for pv in collector.kubectl_items("persistentvolumes", None, errors, cluster_scoped=True, managed_fields=False):
        spec, status = pv.get("spec") or {}, pv.get("status") or {}
        ref = spec.get("claimRef") or {}
        if (ref.get("namespace"), ref.get("name")) in claims or status.get("phase") not in ("Bound", None):
            lines.append(
                f"PV {pv['metadata'].get('name')}: {status.get('phase')}, claim {ref.get('namespace')}/{ref.get('name')}, "
                f"reclaim {spec.get('persistentVolumeReclaimPolicy')}, class {spec.get('storageClassName')}"
            )
    scs = collector.kubectl_items("storageclasses", None, errors, cluster_scoped=True, managed_fields=False)
    if scs:
        lines.append(
            "StorageClasses: "
            + ", ".join(
                f"{s['metadata'].get('name')} ({s.get('provisioner')}, {s.get('volumeBindingMode')}"
                + (
                    ", default"
                    if (s["metadata"].get("annotations") or {}).get("storageclass.kubernetes.io/is-default-class")
                    == "true"
                    else ""
                )
                + ")"
                for s in scs
            )
        )
    return _finish(lines, f"no PVCs in {', '.join(_namespaces(ctx))}", errors)


_GROUPS: dict[str, Callable[[AgentContext], list[str]]] = {
    "admission": _admission,
    "quota": _quota,
    "network": _network,
    "rbac": _rbac,
    "dns": _dns,
    "nodes": _nodes,
    "storage": _storage,
}


def run_cluster_objects(ctx: AgentContext, group: str) -> ToolResult:
    """cluster_objects: one group of cluster-wide objects (admission, quota, network, rbac, dns, nodes, storage)."""
    if group not in _GROUPS:
        raise ValueError(f"unknown cluster group {group}; expected one of {', '.join(CLUSTER_GROUPS)}")
    return ToolResult(lines=_GROUPS[group](ctx))
