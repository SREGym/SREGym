"""client_check: from a client's pod, whether a dependency's name resolves and its port accepts connections."""

from __future__ import annotations

import re

from clients.jev_agent.tools import _common, _podexec
from clients.jev_agent.tools._common import _comp, _components
from clients.jev_agent.tools._podexec import ExecUnavailable, _missing_tool, _ready_pod
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag import collector, probes

_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]*:[0-9a-fA-F:]+)\b")


def _parse_edge(target: str) -> tuple[str, str]:
    """(client, dependency) from a 'client->dependency' target; ValueError on any other form."""
    if "->" not in target:
        raise ValueError(f"edge target must be 'client->target', got {target!r}")
    client, dep = (part.strip() for part in target.split("->", 1))
    return client, dep


def _check_sources(ctx: AgentContext, client_cid: str, dep_cid: str) -> list[tuple[str, str, str | None]]:
    """(component, pod, container) to run a connectivity check from: the client first, then other ready pods in
    its namespace (clients of the target first). The check asks "can the target be reached from this namespace",
    which any healthy pod there can answer when the client itself cannot host a command."""
    comps = _components(ctx)
    client, dep = comps.get(client_cid) or {}, comps.get(dep_cid) or {}
    out: list[tuple[str, str, str | None]] = []
    pod, container = _ready_pod(client)
    if pod:
        out.append((client_cid, pod, container))
    ns = client.get("namespace")
    others = [
        cid
        for cid, c in comps.items()
        if cid not in (client_cid, dep_cid) and c.get("namespace") == ns and not c.get("init")
    ]
    users = set(dep.get("clients") or [])
    others.sort(key=lambda cid: (cid not in users, cid))
    for cid in others:
        pod, container = _ready_pod(comps[cid])
        if pod:
            out.append((cid, pod, container))
        if len(out) >= 4:
            break
    return out


def _edge_applicable(ctx: AgentContext, target: str) -> bool:
    """Whether the target names two different components and some pod can run the check."""
    try:
        client, dep = _parse_edge(target)
    except ValueError:
        return False
    comps = _components(ctx)
    return client in comps and dep in comps and client != dep and bool(_check_sources(ctx, client, dep))


def _resolve(ns: str, pod: str, container: str | None, host: str) -> tuple[str | None, str]:
    """(address, how) for a host name resolved inside the pod with getent, else nslookup; the address is None, with
    the reason, when the name does not resolve or the image has no resolver tool."""
    rc, text = _podexec._exec(ns, pod, container, ["getent", "hosts", host])
    if not _missing_tool(rc, text) or rc == 2:
        if rc == 0:
            m = _IP_RE.search(text)
            return (m.group(1) if m else None), "getent"
        return None, f"getent rc={rc}"
    rc, text = _podexec._exec(ns, pod, container, ["nslookup", host])
    if _missing_tool(rc, text) and "server" not in text.lower():
        return None, "unavailable-tool"
    after_name = text.split("Name:", 1)[1] if "Name:" in text else ""
    m = _IP_RE.search(after_name)
    if m:
        return m.group(1), "nslookup"
    return None, "nslookup: " + collector.trim(text.splitlines()[-1] if text else f"rc={rc}", 80)


def _connect(ns: str, pod: str, container: str | None, host: str, port: int) -> str:
    """'ok', 'refused', 'timeout', 'failed ...' or 'unavailable-tool' for a TCP connection from the pod to host:port,
    made with bash, sh or nc, whichever the image has."""
    attempts = (
        ["timeout", "3", "bash", "-c", f"</dev/tcp/{host}/{port}"],
        ["timeout", "3", "sh", "-c", f"</dev/tcp/{host}/{port}"],
        ["nc", "-z", "-w3", host, str(port)],
    )
    for cmd in attempts:
        rc, text = _podexec._exec(ns, pod, container, cmd)
        low = text.lower()
        if _missing_tool(rc, text) or "cannot create" in low or "directory nonexistent" in low or "no such file" in low:
            continue
        if rc == 0:
            return "ok"
        if "refused" in low:
            return "refused"
        if rc == 124 or "timed out" in low or "timeout" in low:
            return "timeout"
        return f"failed rc={rc} {collector.trim(text, 60)}".strip()
    return "unavailable-tool"


def _check_from(ctx: AgentContext, ns: str, pod: str, container: str | None, dep: dict) -> tuple[list[str], list]:
    """DNS and TCP checks toward dep's Services from one pod. Raises ExecUnavailable when the pod cannot run them."""
    targets = []
    for svc_name in [s.get("name") for s in dep.get("services") or [] if s.get("name")][:2]:
        try:
            svc = _common._get_json(["get", "service", svc_name, "-n", dep["namespace"], "-o", "json"])
            spec = svc.get("spec") or {}
            port = next((p.get("port") for p in spec.get("ports") or [] if p.get("protocol", "TCP") == "TCP"), None)
            targets.append((svc_name, spec.get("clusterIP"), port))
        except Exception as exc:  # noqa: BLE001 - a missing Service is a fact
            targets.append((svc_name, f"unreadable ({collector.trim(str(exc), 60)})", None))
    if not targets:
        port = next((p for c in dep.get("containers") or [] for p in c.get("ports") or []), None)
        targets.append((dep["name"], None, port))
    lines = []
    for svc_name, cluster_ip, port in targets:
        host = f"{svc_name}.{dep['namespace']}.svc.cluster.local"
        resolved, how = _resolve(ns, pod, container, host)
        dest = (
            cluster_ip
            if cluster_ip and cluster_ip != "None" and not str(cluster_ip).startswith("unreadable")
            else resolved
        )
        connect = _connect(ns, pod, container, dest, port) if dest and port else "not attempted (no address or port)"
        lines.append(
            f"from {pod}: {host} resolves to {resolved or 'nothing'} ({how}); Service ClusterIP {cluster_ip}; "
            f"TCP {dest}:{port} -> {connect}"
        )
    return lines, targets


def run_client_check(ctx: AgentContext, target: str) -> ToolResult:
    """client_check: from the client's pod, whether the dependency's name resolves and its port accepts connections."""
    client_cid, dep_cid = _parse_edge(target)
    client, dep = _comp(ctx, client_cid), _comp(ctx, dep_cid)
    comps = _components(ctx)
    notes: list[str] = []
    for cid, pod, container in _check_sources(ctx, client_cid, dep_cid):
        ok, why = probes.headroom_ok(comps[cid], pod, container, probes.NATIVE_TOOL_BYTES)
        if not ok:
            notes.append(f"{pod}: {why}")
            continue
        try:
            lines, targets = _check_from(ctx, comps[cid]["namespace"], pod, container, dep)
        except ExecUnavailable as exc:  # e.g. the container is restarting: try another pod
            notes.append(str(exc)[:120])
            continue
        if cid != client_cid:
            lines.insert(0, f"{client_cid} cannot run commands now; tested from {cid} in the same namespace instead")
        return ToolResult(lines=lines, raw={"pod": pod, "source": cid, "targets": targets})
    return ToolResult(
        lines=[f"no pod in {client.get('namespace')} could host the check: " + ("; ".join(notes) or "none ready")],
        error="no pod to test from",
    )
