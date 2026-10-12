"""backend_probe: a read-only health command inside a data store, or consumer-group progress for Kafka."""

from __future__ import annotations

import copy
import time
from datetime import timedelta
from types import SimpleNamespace

from clients.jev_agent.roles import backend_role
from clients.jev_agent.tools import _podexec
from clients.jev_agent.tools._common import _comp, _components, _secretish, _snapshot_obj
from clients.jev_agent.tools._podexec import ExecUnavailable, _ready_pod
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag import collector, investigate, probes
from clients.jev_diag.timeutil import now

PROBED_ROLES = ("redis", "postgresql", "mongodb", "mysql", "kafka")


def _backend_applicable(ctx: AgentContext, cid: str) -> bool:
    """Whether the component is a data store or queue that backend_probe has commands for."""
    comp = _components(ctx).get(cid)
    return comp is not None and backend_role(comp) in PROBED_ROLES


def _kafka_bootstrap(ctx: AgentContext, ns: str) -> tuple[str | None, str | None]:
    """(broker component id, "service.ns.svc.cluster.local:port") of a Kafka-role component with a Service."""
    for cid, comp in _components(ctx).items():
        if comp.get("namespace") != ns or backend_role(comp) != "kafka":
            continue
        for svc in comp.get("services") or []:
            ports = []
            for spec in svc.get("ports") or []:
                head = str(spec).split("->", 1)[0].split()[-1]
                if head.isdigit():
                    ports.append(int(head))
            if svc.get("name") and ports:
                port = 9092 if 9092 in ports else ports[0]
                return cid, f"{svc['name']}.{ns}.svc.cluster.local:{port}"
    return None, None


# Kafka command-line tools start a JVM. With the heap bounded and the broker's own agent options cleared (as
# SREGym's own Kafka fault injector runs them) the process needs ~100-150Mi RSS, so this much headroom suffices.
KAFKA_TOOL_ENV = ["env", "KAFKA_HEAP_OPTS=-Xms32m -Xmx64m", "KAFKA_OPTS=", "KAFKA_JMX_OPTS="]
BOUNDED_JVM_TOOL_BYTES = 256 * 1024**2
PEEK_TIMEOUT_MS = 8000


def _kafka_sample_from(ns: str, pod: str, container: str | None, bootstrap: str) -> tuple[str | None, list[dict]]:
    """(the consumer-group tool that answered, its rows) from one `--describe --all-groups` run in the pod, trying
    each known tool path; (None, []) when the image has none."""
    for binary in probes._KAFKA_GROUP_COMMANDS:
        cmd = [*KAFKA_TOOL_ENV, binary, "--bootstrap-server", bootstrap, "--describe", "--all-groups"]
        rc, text = _podexec._exec(ns, pod, container, cmd)
        if rc == 0 and "GROUP" in text:
            return binary, probes.parse_consumer_groups(text)
        if not probes._missing_binary(rc, text):
            return binary, []
    return None, []


def _peek_stuck_records(
    ns: str, pod: str, container: str | None, groups_binary: str, bootstrap: str, f: dict
) -> list[str]:
    """Read-only look (no consumer group) at the record a stalled consumer is stuck on and the one after it."""
    binary = groups_binary.replace("kafka-consumer-groups", "kafka-console-consumer")
    cmd = [
        *KAFKA_TOOL_ENV,
        binary,
        "--bootstrap-server",
        bootstrap,
        "--topic",
        str(f["topic"]),
        "--partition",
        str(f["partition"]),
        "--offset",
        str(f["offset"]),
        "--max-messages",
        "2",
        "--timeout-ms",
        str(PEEK_TIMEOUT_MS),
        "--property",
        "print.offset=true",
    ]
    try:
        rc, text = _podexec._exec(ns, pod, container, cmd)
    except ExecUnavailable as exc:
        return [f"could not read the record at the stalled offset: {str(exc)[:80]}"]
    records = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith(("Processed a total", "[", "WARN"))]
    if rc not in (0, None) and not records:
        return [f"could not read the record at the stalled offset (rc={rc}): {collector.trim(text, 120)}"]
    if not records:
        return [f"no record returned at topic {f['topic']} partition {f['partition']} offset {f['offset']}"]
    shown = " || ".join(collector.trim(r, 200) for r in records[:2])
    return [
        f"records from topic {f['topic']} partition {f['partition']} starting at the stalled offset {f['offset']} "
        f"(read-only, no consumer group): {shown}"
    ]


def _kafka_lines(ctx: AgentContext, cid: str) -> tuple[list[str], dict]:
    """Consumer-group positions sampled twice. The admin tool starts a JVM, so it runs only where the memory
    headroom allows: the target pod first, then the broker, then other Kafka-client pods, always against the
    broker's Service address (a client pod has no broker on localhost)."""
    comps = _components(ctx)
    comp = comps[cid]
    ns = comp["namespace"]
    broker_cid, bootstrap = _kafka_bootstrap(ctx, ns)
    if not bootstrap:
        return ["no Kafka broker Service found to query"], {}
    order = [cid, broker_cid] + [
        c for c, x in comps.items() if backend_role(x) == "kafka" and c not in (cid, broker_cid)
    ]
    skipped, source = [], None
    for cand in [c for c in dict.fromkeys(order) if c in comps]:
        pod, container = _ready_pod(comps[cand])
        if not pod:
            continue
        ok, why = probes.headroom_ok(comps[cand], pod, container, BOUNDED_JVM_TOOL_BYTES)
        if not ok:
            skipped.append(f"{cand}: {why}")
            continue
        try:
            binary, rows = _kafka_sample_from(ns, pod, container, bootstrap)
        except ExecUnavailable as exc:
            skipped.append(f"{cand}: {str(exc)[:80]}")
            continue
        if binary is None:
            skipped.append(f"{cand}: no consumer-group tool in the image")
            continue
        source = (cand, pod, container, binary)
        break
    if source is None:
        return ["consumer-group sampling not possible: " + ("; ".join(skipped) or "no ready pod")], {"skipped": skipped}
    cand, pod, container, binary = source
    interval = float(ctx.extra.get("broker_interval", probes.DEFAULT_BROKER_INTERVAL))
    first_at = now()
    before = {broker_cid or cand: {"at": first_at.isoformat(), "pod": pod, "rows": rows}}
    time.sleep(interval)
    _, rows2 = _kafka_sample_from(ns, pod, container, bootstrap)
    second_at = max(now(), first_at + timedelta(seconds=interval))
    after = {broker_cid or cand: {"at": second_at.isoformat(), "pod": pod, "rows": rows2}}
    snap = _snapshot_obj(ctx)
    scratch = SimpleNamespace(components=copy.deepcopy(snap.components), cluster={})
    findings = probes.apply_broker_progress(scratch, before, after)
    groups = sorted({r.get("GROUP") for r in rows2 if r.get("GROUP")})
    lines = [
        f"sampled consumer groups from {pod} ({cand}) via {bootstrap} twice {interval:.0f}s apart: {len(groups)} "
        f"group(s) {groups[:4]}, {len(rows2)} partition row(s)"
    ]
    for f in findings[:4]:
        lines.append(
            f"group {f['group']} topic {f['topic']} partition {f['partition']}: committed offset {f['offset']} unchanged "
            f"over {f['interval_s']}s while log end moved {f['log_end'][0]}->{f['log_end'][1]}; consumer "
            f"{f.get('consumer') or 'not mapped to a component'}"
        )
    if not findings:
        lines.append("no partition kept a fixed committed offset while its log end advanced")
    else:
        lines += _peek_stuck_records(ns, pod, container, binary, bootstrap, findings[0])
    lagging = sorted(
        ((int(r["LAG"]), r) for r in rows2 if str(r.get("LAG") or "").isdigit() and int(r["LAG"]) > 0),
        key=lambda x: -x[0],
    )
    for lag, r in lagging[:2]:
        lines.append(
            f"lag {lag}: group {r.get('GROUP')} topic {r.get('TOPIC')} partition {r.get('PARTITION')} "
            f"(offset {r.get('CURRENT-OFFSET')}, log end {r.get('LOG-END-OFFSET')}, consumer {r.get('CONSUMER-ID')})"
        )
    return lines, {"findings": findings, "rows": rows2[:50], "source": cand}


def _health_probe(comp: dict, cid: str, role: str) -> tuple[list[str], dict]:
    """The role's read-only health command inside the backend (jev_diag.probes command table and meanings).

    Commands are filled with str.replace, not str.format, because some templates contain literal braces.
    """
    commands = probes._BACKEND_COMMANDS.get(role) or ()
    pod, container = probes._pick_pod(comp)
    if not pod:
        return [f"{cid} has no ready running pod to probe"], {}
    port = probes._port(comp, container, role)
    tried = []
    for template, need in commands:
        ok, why = probes.headroom_ok(comp, pod, container, need)
        if not ok:
            tried.append(f"{template[0]} skipped ({why})")
            continue
        command = [part.replace("{port}", str(port)) for part in template]
        rc, text = _podexec._exec(comp["namespace"], pod, container, command)
        if probes._missing_binary(rc, text) and "NOAUTH" not in text:
            tried.append(f"{template[0]} not available")
            continue
        meaning = next((m for rx, m in probes._MEANINGS if rx.search(text)), "unrecognised output")
        line = f"`{' '.join(command)}` in {pod} (rc={rc}) returned '{collector.trim(text, 140)}': {meaning}"
        return [line], {
            "command": command,
            "pod": pod,
            "rc": rc,
            "output": collector.trim(text, 400),
            "meaning": meaning,
        }
    return [f"no {role} health command could run in {pod}: " + ("; ".join(tried) or "none defined")], {}


def _credential_settings(ctx: AgentContext, cid: str) -> str:
    """Names of credential-like env vars and the Secrets a workload reads (values are never read)."""
    comp = _comp(ctx, cid)
    try:
        detail = investigate.workload_detail(comp["kind"], comp["name"], comp["namespace"])
    except Exception as exc:  # noqa: BLE001
        return f"{cid}: spec unreadable ({collector.trim(str(exc), 60)})"
    names, secrets = [], []
    for c in (detail.get("containers") or []) + (detail.get("init_containers") or []):
        for e in c.get("env") or []:
            if _secretish(e.get("name")) or str(e.get("from", "")).startswith("secret:"):
                names.append(e.get("name"))
            if str(e.get("from", "")).startswith("secret:"):
                secrets.append(str(e["from"]).split(":", 1)[1].split(".")[0])
        secrets += [n for n in c.get("env_from") or [] if n and _secretish(n)]
    for v in detail.get("volumes") or []:
        if v.get("secret"):
            secrets.append((v["secret"] or {}).get("secretName") or v.get("name"))
    parts = []
    parts.append("credential-like env " + (", ".join(dict.fromkeys(n for n in names if n)) or "none"))
    parts.append("Secrets " + (", ".join(dict.fromkeys(s for s in secrets if s)) or "none"))
    return f"{cid}: " + "; ".join(parts)


def run_backend_probe(ctx: AgentContext, cid: str) -> ToolResult:
    """backend_probe: a read-only health command inside a database or queue (consumer progress for Kafka)."""
    comp = _comp(ctx, cid)
    role = backend_role(comp)
    lines, raw = _kafka_lines(ctx, cid) if role == "kafka" else _health_probe(comp, cid, role or "")
    if role != "kafka":
        clients = [c for c in comp.get("clients") or [] if c in _components(ctx)][:2]
        settings = [_credential_settings(ctx, cid)] + [_credential_settings(ctx, c) for c in clients]
        lines.append("credential settings in specs (names only): server " + " | clients ".join(settings))
    return ToolResult(lines=lines, raw=raw)
