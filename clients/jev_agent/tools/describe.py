"""describe: a component's pods, containers, controller status and spec."""

from __future__ import annotations

import logging

from clients.jev_agent.tools import _common
from clients.jev_agent.tools._common import (
    CAUSELESS_REASONS,
    MESSAGE_CHARS,
    REVISION_ITEMS,
    _ago,
    _comp,
    _ends,
    _join,
    _secretish,
    _warmup_crash,
    _when,
)
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag import changes as change_log
from clients.jev_diag import collector, investigate
from clients.jev_diag.checks import describe_probe, json_short

logger = logging.getLogger("all.jev_agent.tools")


def _probe_text(kind: str, probe: dict) -> str:
    """'<kind> <probe> (delay, period, timeout, failures)' for one container probe."""
    timing = [
        f"{label}={probe[key]}"
        for key, label in (
            ("initialDelaySeconds", "delay"),
            ("periodSeconds", "period"),
            ("timeoutSeconds", "timeout"),
            ("failureThreshold", "failures"),
        )
        if probe.get(key) is not None
    ]
    return f"{kind} {describe_probe(probe)}" + (f" ({', '.join(timing)})" if timing else "")


def _container_line(c: dict, init: bool) -> str:
    """One line for a container: image, command, args, ports, resources, probes and mounts."""
    image = (c.get("image") or "").split("@")[0]
    parts = [f"{'init ' if init else ''}container {c.get('name')}: image {image}"]
    if c.get("command"):
        parts.append("command " + collector.trim(" ".join(map(str, c["command"])), 80))
    if c.get("args"):
        parts.append("args " + collector.trim(" ".join(map(str, c["args"])), 80))
    if c.get("ports"):
        parts.append(
            "ports " + ",".join(f"{p.get('port')}" + (f"/{p['name']}" if p.get("name") else "") for p in c["ports"])
        )
    res = c.get("resources") or {}
    if res:
        parts.append(
            "resources "
            + " ".join(
                f"{k} {','.join(f'{r}={v}' for r, v in (res.get(k) or {}).items())}"
                for k in ("requests", "limits")
                if res.get(k)
            )
        )
    for kind, probe in (c.get("probes") or {}).items():
        parts.append(_probe_text(kind, probe))
    if c.get("volume_mounts"):
        parts.append(
            "mounts "
            + ",".join(
                f"{m.get('name')}@{m.get('path')}" + (f"[{m['sub_path']}]" if m.get("sub_path") else "")
                for m in c["volume_mounts"]
            )
        )
    return _join(parts)


def _env_line(c: dict) -> str | None:
    """'env <container>: NAME=value, ...' with credential-like values redacted and value sources named; None when the
    container has no env."""
    items = []
    for e in c.get("env") or []:
        name = e.get("name")
        if "from" in e:
            items.append(f"{name}=<{e['from']}>")
        elif "value" in e:
            items.append(f"{name}=<redacted>" if _secretish(name) else f"{name}={collector.trim(str(e['value']), 60)}")
        else:
            items.append(f"{name}=")
    if c.get("env_from"):
        items.append("envFrom " + ",".join(n for n in c["env_from"] if n))
    if not items:
        return None
    return f"env {c.get('name')}: " + ", ".join(items)


def _pod_line(p: dict) -> str:
    """One line for a pod: phase, node, failing conditions, and each container's readiness, restarts, state and last
    termination."""
    if p.get("error"):
        return f"pod {p.get('name')}: {p['error']}"
    parts = [f"pod {p.get('name')}: {p.get('phase')} on {p.get('node') or 'no node'}"]
    bad = [c for c in p.get("conditions") or [] if "=True" not in c]
    if bad:
        parts.append("conditions " + ", ".join(bad))
    for cs in p.get("containers") or []:
        state = cs.get("state") or {}
        text = f"{cs.get('name')} ready={cs.get('ready')} restarts={cs.get('restarts', 0)}"
        if "waiting" in state:
            w = state["waiting"]
            text += f" waiting {w.get('reason')}" + (
                f" ({collector.trim(w.get('message'), 80)})" if w.get("message") else ""
            )
        elif "terminated" in state:
            t = state["terminated"]
            text += f" terminated {t.get('reason')} exit={t.get('exitCode')}"
        last = (cs.get("last_state") or {}).get("terminated")
        if last:
            text += f"; last {last.get('reason')} exit={last.get('exitCode')} at {last.get('finishedAt')}"
            warm = _warmup_crash(p, cs)
            if warm:
                text += f" (start-up crash {warm[0]}s after the pod started; ready and running for {warm[1]}s since)"
        parts.append(text)
    return _join(parts)


def _resource_drift(kind: str, tpl_c: dict, pod_c: dict) -> list[str]:
    """Resource fields that differ between a template container and the running pod's container, leaving out
    Kubernetes' own defaulting (a request left unset takes the value of its limit)."""
    out = []
    t_res, p_res = tpl_c.get("resources") or {}, pod_c.get("resources") or {}
    for part in ("limits", "requests"):
        t, pv = t_res.get(part) or {}, p_res.get(part) or {}
        for key in sorted(set(t) | set(pv)):
            if str(t.get(key)) == str(pv.get(key)):
                continue
            defaulted = (
                part == "requests" and key not in t and str(pv.get(key)) == str((t_res.get("limits") or {}).get(key))
            )
            if not defaulted:
                out.append(f"{part}.{key} {t.get(key) or 'unset'} -> {pv.get(key) or 'unset'}")
    return out


def _admission_drift(comp: dict, pod_name: str) -> str | None:
    """How a running pod differs from its controller's pod template in what admission can change (containers,
    images, resources, env names): such a difference was made when the pod was admitted (a mutating webhook, a
    LimitRange), not by the workload's own spec. None when they agree."""
    kind, name, ns = comp["kind"], comp["name"], comp["namespace"]
    if kind not in ("Deployment", "StatefulSet", "DaemonSet"):
        return None
    obj = _common._get_json(["get", kind.lower(), name, "-n", ns, "-o", "json"])
    tpl = ((obj.get("spec") or {}).get("template") or {}).get("spec") or {}
    pod = (_common._get_json(["get", "pod", pod_name, "-n", ns, "-o", "json"]).get("spec")) or {}
    diffs: list[str] = []
    for group in ("initContainers", "containers"):
        t_by = {c.get("name"): c for c in tpl.get(group) or []}
        p_by = {c.get("name"): c for c in pod.get(group) or []}
        for cname in sorted(set(p_by) - set(t_by)):
            diffs.append(f"container {cname} added ({group}, image {p_by[cname].get('image')})")
        for cname in sorted(set(t_by) & set(p_by)):
            t, pc = t_by[cname], p_by[cname]
            if t.get("image") != pc.get("image"):
                diffs.append(f"container {cname} image {t.get('image')} -> {pc.get('image')}")
            diffs += [f"container {cname} {d}" for d in _resource_drift(kind, t, pc)]
            t_env = {e.get("name") for e in t.get("env") or []}
            added = sorted({e.get("name") for e in pc.get("env") or []} - t_env)
            if added:
                diffs.append(f"container {cname} env added {added[:5]}")
    if not diffs:
        return None
    return (
        f"pod {pod_name} differs from its {kind} template (changed when the pod was admitted, not in the "
        f"{kind}'s own spec): " + "; ".join(diffs[:6])
    )


MAX_CLAIMS = 2  # PersistentVolumeClaims described per workload
CONTROLLER_KINDS = {"Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job", "CronJob"}
MAX_CONTROLLER_LINES = 3


def _template_mix(workload: dict, replicasets: list[dict]) -> list[str]:
    """When ready pods of a Deployment still run an older pod template than the desired one (a paused or stuck
    rollout): how many pods each template serves, and how the older template differs from the current one."""
    serving = [rs for rs in replicasets if ((rs.get("status") or {}).get("readyReplicas") or 0) > 0]
    if len(serving) < 2:
        return []

    def revision(rs: dict) -> int:
        """The ReplicaSet's deployment revision number (0 when absent or malformed)."""
        ann = (rs.get("metadata") or {}).get("annotations") or {}
        try:
            return int(ann.get("deployment.kubernetes.io/revision") or 0)
        except ValueError:
            return 0

    serving.sort(key=revision, reverse=True)
    current, older = serving[0], serving[1:]
    paused = " (rollout paused)" if (workload.get("spec") or {}).get("paused") else ""
    parts = [
        f"revision {revision(rs)} ({(rs.get('metadata') or {}).get('name')}): "
        f"{(rs.get('status') or {}).get('readyReplicas')} ready pod(s)"
        for rs in serving
    ]
    out = [f"ready pods run {len(serving)} pod templates{paused}: " + "; ".join(parts)]
    for rs in older[:1]:
        diff = change_log.template_diff(
            ((rs.get("spec") or {}).get("template")) or {}, ((current.get("spec") or {}).get("template")) or {}
        )
        diff = [d for d in diff if not d.startswith("pod label pod-template-hash")]
        if diff:
            out.append(
                f"pods of the older pod template (revision {revision(rs)}) still serve with settings the current "
                "template changed: " + "; ".join(diff[:REVISION_ITEMS])
            )
    return out


def _controller_failures(comp: dict) -> list[str]:
    """Why the workload's controller cannot create or replace pods, or keeps old ones: its failing status conditions,
    Warning events on it or its ReplicaSets (quotas, admission webhooks and policies reject pods there, before any
    pod exists, so the pods' own events say nothing), and ready pods still on an older pod template."""
    ns, kind, name = comp["namespace"], comp["kind"], comp["name"]
    out: list[str] = []
    workload = _common._get_json(["get", kind.lower(), name, "-n", ns, "-o", "json", "--show-managed-fields"])
    for c in (workload.get("status") or {}).get("conditions") or []:
        failing = (c.get("type") == "ReplicaFailure" and c.get("status") == "True") or (
            c.get("type") in ("Available", "Progressing") and c.get("status") == "False"
        )
        if failing and c.get("message") and c.get("reason") not in CAUSELESS_REASONS:
            out.append(
                f"{kind} condition {c.get('type')}={c.get('status')} {c.get('reason') or ''}: {_ends(c['message'])}"
            )
    owned = {name}
    replicasets: list[dict] = []
    if kind == "Deployment":
        for rs in _common._items("replicasets", ns):
            meta = rs.get("metadata") or {}
            if any(o.get("kind") == kind and o.get("name") == name for o in meta.get("ownerReferences") or []):
                owned.add(meta.get("name"))
                replicasets.append(rs)
    mix = _template_mix(workload, replicasets)
    seen = {line.split(": ", 1)[-1] for line in out}
    for ev in sorted(_common._items("events", ns), key=lambda e: e.get("lastTimestamp") or "", reverse=True):
        obj = ev.get("involvedObject") or {}
        if ev.get("type") != "Warning" or obj.get("kind") not in CONTROLLER_KINDS or obj.get("name") not in owned:
            continue
        message = _ends(ev.get("message") or "")
        if message in seen or message.removeprefix("Error creating: ") in seen:
            continue
        seen.add(message)
        out.append(
            f"controller event Warning {ev.get('reason')} x{ev.get('count') or 1} on {obj.get('kind')} "
            f"{obj.get('name')} {_ago(ev.get('lastTimestamp'))}: {message}"
        )
    return mix + out[:MAX_CONTROLLER_LINES]


def _spec_line(wd: dict) -> str:
    """'spec: ...' with the workload's replicas, strategy, service account, DNS, scheduling and volume settings."""
    sched = [
        f"replicas {wd.get('replicas')}" if wd.get("replicas") is not None else "",
        f"strategy {json_short(wd.get('strategy'), 80)}" if wd.get("strategy") else "",
        f"serviceAccount {wd.get('service_account') or 'default'}",
        f"dnsPolicy {wd['dns_policy']}" if wd.get("dns_policy") else "",
        f"dnsConfig {json_short(wd['dns_config'], 80)}" if wd.get("dns_config") else "",
        f"hostAliases {json_short(wd['host_aliases'], 80)}" if wd.get("host_aliases") else "",
        "hostNetwork true" if wd.get("host_network") else "",
        f"nodeSelector {json_short(wd['node_selector'], 80)}" if wd.get("node_selector") else "",
        f"affinity {collector.trim(wd['affinity'], 80)}" if wd.get("affinity") else "",
        f"tolerations {len(wd['tolerations'])}" if wd.get("tolerations") else "",
        f"restartPolicy {wd['restart_policy']}" if wd.get("restart_policy") else "",
        "volumes "
        + ",".join(
            f"{v.get('name')}:{json_short({k: x for k, x in v.items() if k != 'name'}, 50)}"
            for v in wd.get("volumes") or []
        )
        if wd.get("volumes")
        else "",
    ]
    return "spec: " + _join(sched)


def _claim_lines(wd: dict, ns: str) -> list[str]:
    """The state of the PersistentVolumeClaims the workload mounts (the first MAX_CLAIMS volumes), or why one is
    unreadable."""
    out: list[str] = []
    for v in (wd.get("volumes") or [])[:MAX_CLAIMS]:
        claim = ((v.get("persistentVolumeClaim") or {}).get("claimName")) if isinstance(v, dict) else None
        if not claim:
            continue
        try:
            pvc = _common._get_json(["get", "pvc", claim, "-n", ns, "-o", "json"])
        except Exception as exc:  # noqa: BLE001 - a missing claim is a fact to report
            out.append(
                f"volume {v.get('name')}: PersistentVolumeClaim {claim} unreadable ({collector.trim(str(exc), 80)})"
            )
            continue
        spec, status = pvc.get("spec") or {}, pvc.get("status") or {}
        out.append(
            f"volume {v.get('name')}: PersistentVolumeClaim {claim} {status.get('phase') or '?'}, access modes "
            f"{spec.get('accessModes') or []}, storageClass {spec.get('storageClassName')}; mounted by every pod of "
            f"this workload ({wd.get('replicas')} replicas)"
        )
    return out


def run_describe(ctx: AgentContext, cid: str) -> ToolResult:
    """describe: the component's pods (state, restarts, events), containers (image, settings, resources) and spec."""
    comp = _comp(ctx, cid)
    ns = comp["namespace"]
    wd = investigate.workload_detail(comp["kind"], comp["name"], ns)
    pods, events = investigate.pod_details(_common._current_pod_names(ctx, comp), ns, message_chars=MESSAGE_CHARS)
    lines = [_pod_line(p) for p in pods[:2]] or [f"no pods currently exist for {cid}"]
    if pods and pods[0].get("name") and not pods[0].get("error"):
        try:
            drift = _admission_drift(comp, pods[0]["name"])
        except Exception as exc:  # noqa: BLE001 - the comparison is an extra fact, not the tool's core
            logger.info("admission drift check for %s failed: %s", cid, exc)
            drift = None
        if drift:
            lines.append(drift)
    if events:
        lines.append(
            "events: "
            + " | ".join(
                f"{e.get('type')} {e.get('reason')} x{e.get('count')} {e.get('object')} {_ago(e.get('last'))}: "
                f"{collector.trim(e.get('message'), MESSAGE_CHARS)}"
                for e in events[:3]
            )
        )
    else:
        lines.append("events: none current for these pods")
    try:
        lines += _controller_failures(comp)
    except Exception as exc:  # noqa: BLE001 - an extra fact, not the tool's core
        logger.info("controller status of %s unreadable: %s", cid, exc)
    containers = [(c, True) for c in wd.get("init_containers") or []] + [(c, False) for c in wd.get("containers") or []]
    main = [c for c, init in containers if not init]
    for c, init in sorted(containers, key=lambda x: x[1])[:2]:
        lines.append(_container_line(c, init))
    env = _env_line(main[0]) if main else None
    if env:
        lines.append(env)
    lines.append(_spec_line(wd))
    lines += _claim_lines(wd, ns)
    writers = wd.get("spec_writers") or []
    if writers:
        lines.append(
            f"created {_when(ctx, wd.get('created'))}; spec writers: "
            + ", ".join(f"{w.get('manager')} {w.get('op')} {_when(ctx, w.get('time'))}" for w in writers)
        )
    return ToolResult(lines=lines, raw={"workload": wd, "pods": pods, "events": events})
