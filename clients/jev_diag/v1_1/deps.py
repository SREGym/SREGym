"""What each component depends on, and the facts that follow from it.

A dependency is known from any of three sources:

* address: an env value or command addresses one of the component's Services (checks.resolve_env_hosts);
* traces: the tracing backend observed calls from one to the other (`_observed_calls`, read at collection);
* naming: a data store named after the one service it belongs to (`mongodb-orders` serves `orders`), the usual
  database-per-service convention, taken only when exactly one service fits best. A naming edge is a hint: it is
  shown, but evidence (an address or observed calls) is needed before a failure is attributed along it.

From the graph, three facts are derived:

* A server workload that no Service selects, while the application's other servers each have one and some client
  addresses it or was seen calling it, cannot be reached by that client: a fault of its own, though its pods are
  healthy. Without such a client, a component named for it that fails reaching its dependencies is named as the
  likely client, as a hint.
* An env value that addresses an in-namespace host no Service provides, in an application whose other addresses
  resolve, cannot reach anything: the address is wrong or the Service is gone (a workload of that name decides
  which, and becomes the caller's dependency).
* A component whose still-occurring connection errors name no component is linked to the dependencies that are
  abnormal on their own, so its failure points at them as named errors would.
"""

from __future__ import annotations

import re
from collections import Counter

from clients.jev_diag.v1_1.checks import service_host_index
from clients.jev_diag.v1_1.derive import (
    DEPENDENCY_CLASSES,
    GENERIC_NAME_SUFFIXES,
    OBSERVABILITY_RE,
    ROLE_SYNONYMS,
    _name_aliases,
    backend_role,
    diagnostic_signals,
)

SERVER_KINDS = ("Deployment", "StatefulSet")
MIN_ROUTED_SHARE = 0.75  # the application's servers that have a Service, for a missing one to stand out
MIN_SERVERS = 3
MIN_RESOLVED_ADDRESSES = 2  # in-cluster addresses that do resolve, for an unresolved one to stand out
MAX_BROKEN_DEPENDENCIES = 2  # more than this many abnormal dependencies explain nothing specific
_ROLE_WORDS = set().union(*ROLE_SYNONYMS) | {"db"}
POINTER_PREFIXES = ("errors logged by", "server ", "rejects logins")


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[-_.]", name.lower()) if t}


def naming_edges(components: dict[str, dict]) -> dict[str, set[str]]:
    """client -> data stores named after it: the store's name without its role words names exactly one workload
    best (fewest other words besides generic ones like `service`)."""
    out: dict[str, set[str]] = {}
    for sid, store in components.items():
        if backend_role(store) is None:
            continue
        rest = _name_tokens(store["name"]) - _ROLE_WORDS
        if not rest:
            continue
        fits = []
        for cid, comp in components.items():
            if cid == sid or comp["namespace"] != store["namespace"] or backend_role(comp) is not None:
                continue
            tokens = _name_tokens(comp["name"])
            if rest <= tokens:
                fits.append((len(tokens - rest - GENERIC_NAME_SUFFIXES), cid))
        fits.sort()
        if fits and (len(fits) == 1 or fits[0][0] < fits[1][0]):
            out.setdefault(fits[0][1], set()).add(sid)
    return out


def trace_edges(components: dict[str, dict], observed: list[dict]) -> dict[str, set[str]]:
    """caller -> callees from the tracing backend's call graph, matched by Service or workload name."""
    index = _name_aliases(components)
    out: dict[str, set[str]] = {}
    for edge in observed:
        parent = index.get(str(edge.get("parent") or "").lower())
        child = index.get(str(edge.get("child") or "").lower())
        if parent and child and parent != child:
            out.setdefault(parent, set()).add(child)
    return out


def build_graph(components: dict[str, dict], observed: list[dict]) -> None:
    """Set `_deps` ({dependency: [sources]}) on every component that has dependencies."""
    sources = {
        "address": {cid: set(comp.get("_calls") or []) for cid, comp in components.items()},
        "traces": trace_edges(components, observed),
        "naming": naming_edges(components),
    }
    for comp in components.values():
        comp.pop("_deps", None)
    for source, edges in sources.items():
        for cid, deps in edges.items():
            for dep in deps:
                if dep in components and dep != cid:
                    components[cid].setdefault("_deps", {}).setdefault(dep, []).append(source)


EVIDENCE_SOURCES = ("address", "traces")  # edges a failure may be attributed along; naming alone is a hint


def clients_of(components: dict[str, dict], cid: str) -> list[str]:
    """Components that address `cid` or were seen calling it."""
    return sorted(
        c for c, other in components.items() if set((other.get("_deps") or {}).get(cid) or []) & set(EVIDENCE_SOURCES)
    )


def own_signals(comp: dict) -> list[str]:
    """The component's signals about itself, not what other components say about it."""
    return [s for s in diagnostic_signals(comp) if not s.startswith(POINTER_PREFIXES)]


def flag_unrouted_servers(components: dict[str, dict]) -> None:
    """A server workload that no Service selects, in an application whose other servers have Services."""
    by_ns: dict[str, list[str]] = {}
    for cid, comp in components.items():
        listens = any(c.get("ports") for c in comp.get("containers") or [] if not c.get("init"))
        if comp.get("kind") in SERVER_KINDS and listens and not OBSERVABILITY_RE.search(comp["name"]):
            by_ns.setdefault(comp["namespace"], []).append(cid)
    for servers in by_ns.values():
        routed = [c for c in servers if components[c].get("services")]
        if len(servers) < MIN_SERVERS or len(routed) < MIN_ROUTED_SHARE * len(servers):
            continue
        for cid in servers:
            comp = components[cid]
            if comp.get("services"):
                continue
            clients = clients_of(components, cid)
            ports = sorted({p for c in comp.get("containers") or [] for p in c.get("ports") or []})
            head = (
                f"no Service selects this workload, although its containers listen on {ports} and {len(routed)} of "
                f"the application's {len(servers)} servers have one"
            )
            if clients:
                comp["signals"].append(
                    f"{head}, and {', '.join(clients)} address it or call it: they cannot reach it through a Service"
                )
                continue
            # without an address or an observed call, a component named for it that fails reaching its
            # dependencies is the client it most likely serves (nothing at all: a worker exposing metrics, for one)
            hinted = [c for c in _naming_clients(components, cid) if _fails_dependencies(components[c])]
            if hinted:
                comp["signals"].append(
                    f"{head}; {', '.join(hinted)}, named for it, fails reaching its dependencies and may reach this "
                    "one through a Service it cannot find"
                )


def _naming_clients(components: dict[str, dict], cid: str) -> list[str]:
    return sorted(c for c, other in components.items() if "naming" in ((other.get("_deps") or {}).get(cid) or []))


def _fails_dependencies(comp: dict) -> bool:
    return any(set(f.get("classes") or []) & DEPENDENCY_CLASSES for f in comp.get("log_findings") or [])


def flag_dangling_addresses(components: dict[str, dict], namespaceless_services: list[dict]) -> None:
    """Env addresses of in-namespace hosts that no Service provides, while the application's other addresses resolve.

    A host counts as in-namespace when it has no dot (Kubernetes resolves it in the caller's namespace) or is
    qualified with one of the application's namespaces. A workload named like the host is the intended server: its
    Service is missing, and the caller depends on it.
    """
    index = service_host_index(components)
    known = {str(svc.get("name") or "").lower() for svc in namespaceless_services}
    namespaces = {comp["namespace"] for comp in components.values()}
    workloads = {(comp["namespace"], comp["name"].lower()): cid for cid, comp in components.items()}
    resolved, dangling = 0, []
    for cid, comp in components.items():
        for host in comp.get("_env_addresses") or []:
            labels = host.split(".")
            internal = len(labels) == 1 or (
                labels[1] in namespaces and labels[2:] in ([], ["svc"], ["svc", "cluster", "local"])
            )
            if not internal or labels[0] in ("localhost",) or labels[0].isdigit():
                continue
            qualified = f"{host}.{comp['namespace']}" if len(labels) == 1 else host
            if qualified in index or labels[0] in known:
                resolved += 1
            else:
                ns = labels[1] if len(labels) > 1 else comp["namespace"]
                dangling.append((cid, host, ns, workloads.get((ns, labels[0]))))
    if resolved < MIN_RESOLVED_ADDRESSES:
        return
    for cid, host, ns, server in dangling:
        comp = components[cid]
        target = f"; workload {components[server]['name']} exists but no Service exposes it" if server else ""
        comp["signals"].append(
            f"env value addresses host {host}, but no Service {host.split('.')[0]} exists in namespace {ns}, so "
            f"its calls cannot resolve{target}"
        )
        if server and server != cid:
            comp.setdefault("_deps", {}).setdefault(server, []).append("address")


def link_unnamed_failures(components: dict[str, dict]) -> None:
    """Still-occurring connection errors that name no component point at the dependencies abnormal on their own."""
    for cid, comp in components.items():
        if comp.get("errors_point_to"):
            continue
        unnamed: Counter[str] = Counter()
        for f in comp.get("log_findings") or []:
            if not f.get("targets"):
                for klass in set(f.get("classes") or []) & DEPENDENCY_CLASSES:
                    unnamed[klass] += int(f.get("count") or 1)
        deps = {d: src for d, src in (comp.get("_deps") or {}).items() if set(src) & set(EVIDENCE_SOURCES)}
        broken = [
            d
            for d in sorted(deps)
            if own_signals(components[d])
            or (not components[d].get("pods_not_verified") and not components[d].get("pods"))
        ]
        if not unnamed or not broken or len(broken) > MAX_BROKEN_DEPENDENCIES:
            continue
        comp["errors_point_to"] = broken
        klass, n = unnamed.most_common(1)[0]
        for dep in broken:
            target = components[dep]
            target.setdefault("referenced_by_errors_from", {})[cid] = dict(unnamed)
            target["signals"].append(
                f"errors logged by {cid} ({klass}, {n} lines, still occurring) name no component; {cid} depends on "
                f"this component ({', '.join(deps[dep])}), which is abnormal on its own"
            )


def apply(snapshot) -> None:
    """Every dependency derivation, in order (called from derive.post_process, after the logs are classified)."""
    components = snapshot.components
    build_graph(components, snapshot.cluster.get("_observed_calls") or [])
    flag_dangling_addresses(components, snapshot.cluster.get("services_matching_no_workload") or [])
    flag_unrouted_servers(components)
    link_unnamed_failures(components)
