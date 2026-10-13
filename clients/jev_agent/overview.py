"""The whole-cluster view shown to the decision model, built by whitelisting generic fields of a snapshot.

`build_overview` copies only raw, fault-agnostic facts out of the full jev_diag snapshot: replica and pod
states, event counts, endpoint counts, resource usage, error-log signature counts, alert names, call edges and
change times. It never copies the snapshot's derived judgments (comp["signals"], spec_flags, evidence_kinds,
healthy, findings, blocked_input, cluster findings, stalled consumers, CoreDNS rule matches, webhook/coredns
"affects" lists), because those were produced by fault-specific checks.
"""

from __future__ import annotations

import math
import re
from typing import Any

from clients.jev_agent.roles import backend_role
from clients.jev_diag import changes as change_log
from clients.jev_diag.checks import parse_cpu_millis, parse_memory_bytes
from clients.jev_diag.collector import estimate_tokens, trim
from clients.jev_diag.derive import OBSERVABILITY_RE, TELEMETRY_RE
from clients.jev_diag.timeutil import parse_time, seconds_between

MAX_PODS = 4
MAX_RECENT_CHANGES = 8
MAX_CLUSTER_ITEMS = 6
_STATE_RE = re.compile(
    r"^(?P<container>[^:]+): (?P<what>last terminated|terminated|waiting) (?P<reason>[^\s:]+)"
    r"(?: exit=(?P<exit>-?\d+|None))?"
)
_BENIGN_WAITING = {"ContainerCreating", "PodInitializing"}
# Change kinds whose "affects" list comes from generic selector/reference matching (not from fault checks).
_GENERIC_AFFECT_KINDS = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "Service",
    "NetworkPolicy",
    "ConfigMap",
    "Secret",
    "Role",
    "ClusterRole",
}

# Abnormality score weights (component_scores). Each term is capped so no single kind of evidence dominates.
W_NOT_READY = 3.0  # x fraction of desired replicas not ready
W_NONE_READY = 1.0  # extra when desired > 0 and none is ready
W_RESTARTS = 0.6  # x log2(1 + restarts), capped at CAP_RESTARTS
CAP_RESTARTS = 2.0
W_WAITING = 2.0  # any container waiting for a non-benign reason (CrashLoopBackOff, ImagePullBackOff, ...)
W_TERMINATED = 1.0  # any non-zero last/current exit code or OOMKilled
W_PENDING = 1.5  # any pod not Running/Succeeded
W_EVENTS = 0.5  # x log2(1 + warning event count), capped at CAP_EVENTS
CAP_EVENTS = 2.0
W_ERRORS = 0.4  # x log2(1 + ongoing error lines), capped at CAP_ERRORS
CAP_ERRORS = 2.0
W_ALERT = 1.5  # per distinct firing alert, capped at CAP_ALERTS
CAP_ALERTS = 3.0
W_NO_ENDPOINTS = 1.5  # a Service of the component has 0 ready endpoints
W_CHANGE = 1.0  # the component's own spec/template changed after creation, or a recent change affects it
W_RESOURCE = 1.0  # memory or cpu usage >= 90% of the limit
W_SHARED_SERVICE = 0.5  # one of the component's Services also selects pods of another workload
W_OLD_TEMPLATE = 1.5  # ready pods still run an older pod template than the desired one (paused or stuck rollout)
ROLLOUT_KINDS = ("Deployment", "StatefulSet")
DEPLOY_PHASE_S = change_log.MIN_CHANGE_SECONDS  # a revision this soon after creation is part of the deployment
OBSERVABILITY_FACTOR = 0.5  # telemetry infrastructure is down-weighted


def snapshot_parts(snapshot: Any) -> tuple[dict, dict[str, dict], dict, str | None]:
    """(app, components, cluster, collected_at) from a ClusterSnapshot or a saved snapshot/state dict."""
    if isinstance(snapshot, dict):
        return (
            snapshot.get("application") or snapshot.get("app") or {},
            snapshot.get("components") or {},
            snapshot.get("cluster") or {},
            snapshot.get("collected_at"),
        )
    return (
        getattr(snapshot, "app", {}) or {},
        getattr(snapshot, "components", {}) or {},
        getattr(snapshot, "cluster", {}) or {},
        getattr(snapshot, "collected_at", None),
    )


def is_observability(comp: dict) -> bool:
    """Whether the component is part of the observability stack (by name only)."""
    # Name only, as jev_diag's tag_roles does: image strings give false hits ("hotel-reservation" contains "otel").
    return bool(OBSERVABILITY_RE.search(comp.get("name") or ""))


def parse_container_state(text: str) -> dict | None:
    """'c: waiting CrashLoopBackOff: msg' / 'c: last terminated OOMKilled exit=137' -> fields."""
    m = _STATE_RE.match(text or "")
    if not m:
        return None
    exit_code = m.group("exit")
    return {
        "container": m.group("container"),
        "what": m.group("what"),
        "reason": m.group("reason"),
        "exit": int(exit_code) if exit_code not in (None, "None") else None,
    }


def settled_pods(comp: dict) -> set[str]:
    """Pods whose restarts the collector judged warm-up churn that has since settled (comp["startup_history"]
    entries read "pod <name>: ...")."""
    out = set()
    for entry in comp.get("startup_history") or []:
        if isinstance(entry, str) and entry.startswith("pod ") and ":" in entry:
            out.add(entry[4:].split(":", 1)[0].strip())
    return out


def _pod_view(pod: dict, settled: bool = False) -> dict:
    """Pod state; restarts and last terminations of a settled pod are kept apart as start-up history."""
    view: dict[str, Any] = {
        "name": pod.get("name"),
        "phase": pod.get("phase"),
        "ready": pod.get("ready"),
        "restarts": 0 if settled else (pod.get("restarts") or 0),
    }
    if settled and pod.get("restarts"):
        view["startup_restarts"] = pod.get("restarts")
    for text in pod.get("container_states") or []:
        st = parse_container_state(text)
        if st is None:
            continue
        if st["what"] == "waiting":
            view.setdefault("waiting", []).append(f"{st['container']}: {st['reason']}")
        elif st["what"] == "last terminated":
            key = "startup_last_terminated" if settled else "last_terminated"
            view.setdefault(key, []).append(f"{st['container']}: {st['reason']} exit={st['exit']}")
        elif st["exit"] not in (0, None):
            view.setdefault("terminated", []).append(f"{st['container']}: {st['reason']} exit={st['exit']}")
    conditions = [c.split(":", 1)[0] for c in pod.get("conditions") or []]
    if conditions:
        view["conditions"] = conditions[:3]
    return view


def _usage(comp: dict) -> list[str]:
    """'<container>: cpu used/limit (N%), mem used/limit (N%)' per container, from its highest usage sample."""
    best: dict[str, dict] = {}
    for u in comp.get("resource_usage") or []:
        name = u.get("container") or "?"
        cur = best.setdefault(name, dict(u))
        if (parse_memory_bytes(u.get("memory")) or 0) > (parse_memory_bytes(cur.get("memory")) or 0):
            cur["memory"] = u.get("memory")
        if (parse_cpu_millis(u.get("cpu")) or 0) > (parse_cpu_millis(cur.get("cpu")) or 0):
            cur["cpu"] = u.get("cpu")
    limits = {c.get("name"): c.get("limits") or {} for c in comp.get("containers") or []}
    out = []
    for name, u in best.items():
        lim = limits.get(name) or {}
        cpu_lim, mem_lim = u.get("cpu_limit") or lim.get("cpu"), u.get("memory_limit") or lim.get("memory")
        cpu = f"cpu {u.get('cpu')}" + (f"/{cpu_lim}" if cpu_lim else "")
        c_used, c_lim = parse_cpu_millis(u.get("cpu")), parse_cpu_millis(cpu_lim)
        if c_used is not None and c_lim:
            cpu += f" ({round(100 * c_used / c_lim)}%)"
        mem = f"mem {u.get('memory')}" + (f"/{mem_lim}" if mem_lim else "")
        m_used, m_lim = parse_memory_bytes(u.get("memory")), parse_memory_bytes(mem_lim)
        if m_used is not None and m_lim:
            mem += f" ({round(100 * m_used / m_lim)}%)"
        out.append(f"{name}: {cpu}, {mem}")
    return out


def _usage_ratio(comp: dict) -> float:
    """Highest usage/limit ratio over containers (cpu or memory), 0 when no limit is known."""
    limits = {c.get("name"): c.get("limits") or {} for c in comp.get("containers") or []}
    best = 0.0
    for u in comp.get("resource_usage") or []:
        lim = limits.get(u.get("container")) or {}
        pairs = (
            (parse_cpu_millis(u.get("cpu")), parse_cpu_millis(u.get("cpu_limit") or lim.get("cpu"))),
            (parse_memory_bytes(u.get("memory")), parse_memory_bytes(u.get("memory_limit") or lim.get("memory"))),
        )
        for used, limit in pairs:
            if used is not None and limit:
                best = max(best, used / limit)
    return best


def _errors(comp: dict, observability: bool) -> dict | None:
    """Counts of the component's ongoing and stopped error signatures with the most frequent ongoing line,
    telemetry-export errors counted apart; None when there are none."""
    ongoing, stopped, telemetry = [], [], 0
    for sig in comp.get("log_signals") or []:
        line = sig.get("line") or ""
        if not observability and TELEMETRY_RE.search(line):
            telemetry += int(sig.get("count") or 1)
            continue
        (stopped if sig.get("state") == "stopped" else ongoing).append(sig)
    if not ongoing and not stopped and not telemetry:
        return None
    out: dict[str, Any] = {
        "ongoing_signatures": len(ongoing),
        "ongoing_lines": sum(int(s.get("count") or 1) for s in ongoing),
    }
    if ongoing:
        top = max(ongoing, key=lambda s: int(s.get("count") or 1))
        out["top"] = trim(top.get("line"), 160)
        if top.get("first_seen_seconds_ago") is not None:
            out["top_first_seen_s_ago"] = top["first_seen_seconds_ago"]
    if stopped:
        out["stopped_signatures"] = len(stopped)
    if telemetry:
        out["telemetry_export_lines"] = telemetry
    return out


def _events(comp: dict) -> dict | None:
    """The component's Warning events: their total count and the two most frequent reasons with a message; None when
    there are none."""
    events = comp.get("warning_events") or []
    if not events:
        return None
    by_reason: dict[str, dict] = {}
    for ev in events:
        rec = by_reason.setdefault(ev.get("reason") or "?", {"count": 0, "message": ev.get("message")})
        rec["count"] += int(ev.get("count") or 1)
    top = sorted(by_reason.items(), key=lambda kv: -kv[1]["count"])[:2]
    return {
        "count": sum(r["count"] for r in by_reason.values()),
        "top": [[reason, r["count"], trim(r["message"], 100)] for reason, r in top],
    }


def _service_index(components: dict[str, dict]) -> dict[str, str]:
    """{"ns/service": component id} over the components' Services."""
    index = {}
    for cid, comp in components.items():
        for svc in comp.get("services") or []:
            if svc.get("name"):
                index.setdefault(f"{comp.get('namespace')}/{svc['name']}", cid)
    return index


def _seconds(later: str | None, earlier: str | None) -> int | None:
    """Seconds from `earlier` to `later`, or None when either is missing."""
    return seconds_between(parse_time(later), parse_time(earlier))


def _links(cid: str, comp: dict, components: dict[str, dict], svc_index: dict[str, str]) -> dict:
    """The component's known dependencies (calls, else the Services it calls) and clients, as overview fields."""
    out: dict[str, Any] = {}
    deps = list(comp.get("_calls") or [])
    if not deps:
        deps = [svc_index[s] for s in comp.get("calls_services") or [] if s in svc_index]
    deps = [d for d in dict.fromkeys(deps) if d in components and d != cid]
    if deps:
        out["depends_on"] = deps
    clients = [c for c in comp.get("clients") or [] if c in components and c != cid]
    if clients:
        out["clients"] = clients
    return out


def _change_facts(comp: dict) -> dict:
    """When the component was last modified and its latest pod-template change (or that the revision came with
    the deployment itself), as overview fields."""
    out: dict[str, Any] = {}
    if comp.get("modified_seconds_ago") is not None:
        out["modified_seconds_ago"] = comp["modified_seconds_ago"]
    after = _seconds(comp.get("modified"), comp.get("created"))
    if after is not None and after >= 10:
        out["modified_after_creation_s"] = after
    tmpl = comp.get("template_changes") or {}
    if tmpl.get("changes"):
        since_creation = _seconds(tmpl.get("changed_at"), comp.get("created"))
        if since_creation is not None and since_creation < DEPLOY_PHASE_S:
            out["deploy_time_revision"] = True  # rolled out with the deployment itself
        else:
            out["template_changed_at"] = tmpl.get("changed_at")
            out["template_changes"] = [trim(c, 120) for c in tmpl["changes"][:3]]
    return out


def _component_view(cid: str, comp: dict, components: dict[str, dict], svc_index: dict[str, str]) -> dict:
    """The overview row of one component: replicas, pods, workload state, events, endpoints, usage, errors, alerts,
    links, changes and role."""
    observability = is_observability(comp)
    replicas = comp.get("replicas") or {}
    pods = comp.get("pods") or []
    settled = settled_pods(comp)
    view: dict[str, Any] = {
        "id": cid,
        "kind": comp.get("kind"),
        "name": comp.get("name"),
        "namespace": comp.get("namespace"),
        "replicas": {"desired": replicas.get("desired", 0), "ready": replicas.get("ready", 0)},
        "pods": [_pod_view(p, p.get("name") in settled) for p in pods[:MAX_PODS]],
    }
    updated = replicas.get("updated")
    if (
        comp.get("kind") in ROLLOUT_KINDS
        and isinstance(updated, int)
        and (replicas.get("ready") or 0) > 0
        and updated < (replicas.get("desired") or 0)
    ):
        view["replicas"]["updated"] = updated  # some ready pods still run an older template
    if len(pods) > MAX_PODS:
        view["pods_total"] = len(pods)
    if comp.get("cronjob"):
        view["cronjob"] = {
            k: comp["cronjob"].get(k)
            for k in ("schedule", "suspend", "active_jobs", "last_schedule", "last_successful")
        }
    if comp.get("job"):
        view["job"] = {k: comp["job"].get(k) for k in ("active", "succeeded", "failed")}
    events = _events(comp)
    if events:
        view["warning_events"] = events
    endpoints = [
        {"service": s.get("name"), "ready": s.get("ready_endpoints", 0), "not_ready": s.get("not_ready_endpoints", 0)}
        for s in comp.get("services") or []
        if s.get("name")
    ]
    if endpoints:
        view["endpoints"] = endpoints
    usage = _usage(comp)
    if usage:
        view["usage"] = usage
    usage_ratio = _usage_ratio(comp)
    if usage_ratio:
        view["max_usage_ratio"] = round(usage_ratio, 2)
    errors = _errors(comp, observability)
    if errors:
        view["errors"] = errors
    alerts = sorted(set(comp.get("alerts") or []))
    if alerts:
        view["alerts"] = alerts
    view.update(_links(cid, comp, components, svc_index))
    view.update(_change_facts(comp))
    if observability:
        view["role"] = "observability"
    else:
        role = backend_role(comp)
        if role:
            view["role"] = role
    return view


def _recent_changes(cluster: dict, app_namespaces: list[str]) -> list[dict]:
    """The cluster's recent changes (up to MAX_RECENT_CHANGES) with their timing and changed fields, and the
    application workloads a generic object affects."""
    raw_by_object = {c.get("object"): c for c in cluster.get("_changes") or []}
    out = []
    for change in (cluster.get("recent_changes") or [])[:MAX_RECENT_CHANGES]:
        rec: dict[str, Any] = {"object": change.get("object")}
        for key in ("seconds_ago", "seconds_before_symptoms", "modified_after_creation_s", "created_after_targets_s"):
            if change.get(key) is not None:
                rec[key] = change[key]
        if change.get("fields"):
            rec["fields"] = [trim(f, 80) for f in change["fields"][:4]]
        raw = raw_by_object.get(change.get("object")) or {}
        if raw.get("kind") in _GENERIC_AFFECT_KINDS and (raw.get("namespace") in app_namespaces):
            affects = [a for a in raw.get("affects") or [] if a != change.get("object")]
            if affects:
                rec["affects"] = affects[:6]
        if change.get("role") == "observability":
            rec["role"] = "observability"
        out.append(rec)
    return out


def _shared_services(components: dict[str, dict], comps: dict[str, dict]) -> tuple[dict[str, list[str]], list[dict]]:
    """({"ns/service": workloads it selects}, Services selecting several workloads), noting the latter on each
    workload's view."""
    # which workloads each Service selects (a fact from the collector's selector matching)
    selected_by: dict[str, list[str]] = {}
    for cid, comp in components.items():
        for svc in comp.get("services") or []:
            if svc.get("name"):
                selected_by.setdefault(f"{comp.get('namespace')}/{svc['name']}", []).append(cid)
    shared = []
    for key, cids in sorted(selected_by.items()):
        if len(cids) < 2:
            continue
        shared.append({"service": key, "selects": sorted(cids)})
        for cid in cids:
            others = sorted(c for c in cids if c != cid)
            comps[cid].setdefault("shared_service", []).append(
                f"{key.split('/', 1)[1]} also selects {', '.join(others)}"
            )
    return selected_by, shared


def _mark_changed(cluster: dict, namespaces: list[str], comps: dict[str, dict], selected_by: dict) -> list[dict]:
    """The recent changes, each marking the components it touches (a changed Service marks the workloads it
    selects) with recent_change."""
    recent = _recent_changes(cluster, namespaces)
    for change in recent:
        obj = change.get("object") or ""
        targets = [obj, *(change.get("affects") or [])]
        parts = obj.split("/")
        if len(parts) == 3 and parts[0] == "service":
            targets += selected_by.get(f"{parts[1]}/{parts[2]}", [])
            change.setdefault("selects", selected_by.get(f"{parts[1]}/{parts[2]}", [])[:6])
        for cid in targets:
            if cid in comps:
                comps[cid]["recent_change"] = True
    return recent


def _unattributed_alerts(cluster: dict, comps: dict[str, dict]) -> list[dict]:
    """Firing alerts not already shown on a component, with their identifying labels."""
    attributed = {a for c in comps.values() for a in c.get("alerts") or []}
    alerts = []
    for alert in cluster.get("firing_alerts") or []:
        if alert.get("alertname") in attributed:
            continue
        labels = alert.get("labels") or {}
        alerts.append(
            {
                "name": alert.get("alertname"),
                "labels": {
                    k: labels[k] for k in ("namespace", "service", "deployment", "pod", "severity") if k in labels
                },
                "summary": trim(alert.get("summary"), 120),
                "active_since": alert.get("active_since"),
            }
        )
    return alerts


def build_overview(snapshot: Any) -> dict:
    """Whitelisted, fault-agnostic view of the full collect_snapshot output (see module docstring)."""
    app, components, cluster, collected_at = snapshot_parts(snapshot)
    namespaces = list(app.get("namespaces") or cluster.get("namespaces") or [])
    svc_index = _service_index(components)
    comps = {cid: _component_view(cid, comp, components, svc_index) for cid, comp in components.items()}

    selected_by, shared = _shared_services(components, comps)
    recent = _mark_changed(cluster, namespaces, comps, selected_by)
    alerts = _unattributed_alerts(cluster, comps)
    nodes = [
        {"name": n.get("name"), "problems": n.get("problems") or [], "taints": n.get("taints") or []}
        for n in cluster.get("nodes") or []
        if n.get("problems") or n.get("taints")
    ]
    warnings = [
        {
            "reason": e.get("reason"),
            "object": e.get("object"),
            "count": e.get("count"),
            "message": trim(e.get("message"), 100),
        }
        for e in (cluster.get("warning_events_not_attributed") or [])[:MAX_CLUSTER_ITEMS]
    ]
    unowned_services = [
        {"service": s.get("name"), "ready": s.get("ready_endpoints", 0)}
        for s in (cluster.get("services_matching_no_workload") or [])[:MAX_CLUSTER_ITEMS]
    ]
    unowned_pods = [
        {k: p.get(k) for k in ("name", "phase", "ready", "restarts")}
        for p in (cluster.get("unhealthy_pods_not_attributed") or [])[:MAX_CLUSTER_ITEMS]
    ]
    overview = {
        "app": {"name": app.get("name"), "namespaces": namespaces, "description": trim(app.get("description"), 200)},
        "collected_at": collected_at,
        "components": comps,
        "cluster": {
            "symptom_onset": cluster.get("symptom_onset"),
            "symptom_onset_source": cluster.get("symptom_onset_source"),
            "recent_changes": recent,
            "alerts_not_attributed": alerts,
            "node_problems": nodes,
            "warnings_not_attributed": warnings,
            "services_selecting_no_workload": unowned_services,
            "services_selecting_multiple_workloads": shared[:MAX_CLUSTER_ITEMS],
            "pods_not_attributed": unowned_pods,
        },
    }
    return overview


# --------------------------------------------------------------------------- scoring


def component_scores(overview: dict) -> dict[str, float]:
    """Generic abnormality score per component (higher = more abnormal); see the W_* weights above."""
    scores: dict[str, float] = {}
    for cid, c in (overview.get("components") or {}).items():
        s = 0.0
        desired, ready = (c.get("replicas") or {}).get("desired", 0) or 0, (c.get("replicas") or {}).get("ready", 0)
        if desired > 0:
            missing = max(0, desired - (ready or 0))
            s += W_NOT_READY * missing / desired
            if not ready:
                s += W_NONE_READY
        pods = c.get("pods") or []
        restarts = sum(int(p.get("restarts") or 0) for p in pods)
        s += min(CAP_RESTARTS, W_RESTARTS * math.log2(1 + restarts))
        waiting = [w.split(": ", 1)[-1] for p in pods for w in p.get("waiting") or []]
        if any(w not in _BENIGN_WAITING for w in waiting):
            s += W_WAITING
        terminated = [t for p in pods for t in (p.get("last_terminated") or []) + (p.get("terminated") or [])]
        if any("exit=0" not in t for t in terminated):
            s += W_TERMINATED
        if any(p.get("phase") not in ("Running", "Succeeded") for p in pods):
            s += W_PENDING
        events = (c.get("warning_events") or {}).get("count") or 0
        s += min(CAP_EVENTS, W_EVENTS * math.log2(1 + events))
        lines = (c.get("errors") or {}).get("ongoing_lines") or 0
        s += min(CAP_ERRORS, W_ERRORS * math.log2(1 + lines))
        s += min(CAP_ALERTS, W_ALERT * len(c.get("alerts") or []))
        if any(e.get("ready", 0) == 0 for e in c.get("endpoints") or []):
            s += W_NO_ENDPOINTS
        if c.get("recent_change") or c.get("modified_after_creation_s") or c.get("template_changes"):
            s += W_CHANGE
        if (c.get("max_usage_ratio") or 0) >= 0.9:
            s += W_RESOURCE
        if c.get("shared_service"):
            s += W_SHARED_SERVICE
        if "updated" in (c.get("replicas") or {}):
            s += W_OLD_TEMPLATE
        if c.get("role") == "observability":
            s *= OBSERVABILITY_FACTOR
        scores[cid] = round(s, 3)
    return scores


def ranked_components(overview: dict) -> list[str]:
    """Component ids from most to least abnormal."""
    scores = component_scores(overview)
    return sorted(scores, key=lambda cid: (-scores[cid], cid))


def neighbors(overview: dict, cid: str) -> list[str]:
    """One-hop dependencies and clients of a component (ids present in the overview)."""
    comps = overview.get("components") or {}
    comp = comps.get(cid) or {}
    out = list(comp.get("depends_on") or []) + list(comp.get("clients") or [])
    for other_id, other in comps.items():
        if cid in (other.get("depends_on") or []) or cid in (other.get("clients") or []):
            out.append(other_id)
    return [n for n in dict.fromkeys(out) if n in comps and n != cid]


# --------------------------------------------------------------------------- rendering


def _pod_line(p: dict) -> str:
    """One rendered line for a pod: phase, readiness, restarts, container states, settled start-up restarts and
    conditions."""
    text = f"{p.get('name')}: {p.get('phase')} {p.get('ready')} restarts={p.get('restarts', 0)}"
    for key in ("waiting", "terminated", "last_terminated"):
        if p.get(key):
            text += f"; {key.replace('_', ' ')} " + ", ".join(p[key])
    if p.get("startup_restarts"):
        text += f"; {p['startup_restarts']} start-up restarts during warm-up, settled since"
    if p.get("conditions"):
        text += "; " + ", ".join(p["conditions"])
    return text


def _render_component(c: dict, multi_ns: bool, level: int) -> dict:
    """level 0 = full, 1 = no pod detail/usage text, 2 = one-line essentials."""
    rep = c.get("replicas") or {}
    out: dict[str, Any] = {"id": c["id"]}
    if multi_ns:
        out["ns"] = c.get("namespace")
    out["ready"] = f"{rep.get('ready', 0)}/{rep.get('desired', 0)}"
    if "updated" in rep:
        out["rollout"] = f"only {rep['updated']}/{rep.get('desired', 0)} pods run the current pod template"
    if c.get("role"):
        out["role"] = c["role"]
    pods = c.get("pods") or []
    restarts = sum(int(p.get("restarts") or 0) for p in pods)
    if level == 0:
        notable = [p for p in pods if p.get("restarts") or p.get("waiting") or p.get("phase") != "Running"]
        notable += [p for p in pods if p.get("last_terminated") and p not in notable]
        if notable:
            out["pods"] = [_pod_line(p) for p in notable[:3]]
        if c.get("usage"):
            out["usage"] = c["usage"][:3]
    elif restarts:
        out["restarts"] = restarts
    if c.get("cronjob") and level < 2:
        out["cronjob"] = {k: v for k, v in c["cronjob"].items() if v not in (None, False)}
    if c.get("job"):
        out["job"] = c["job"]
    if c.get("warning_events"):
        ev = c["warning_events"]
        out["warning_events"] = ev if level == 0 else {"count": ev["count"], "top": [t[:2] for t in ev["top"]]}
    bad_eps = [e for e in c.get("endpoints") or [] if e.get("ready", 0) == 0 or e.get("not_ready")]
    if bad_eps:
        out["endpoints"] = [f"{e['service']}: ready {e['ready']}, not ready {e['not_ready']}" for e in bad_eps]
    if c.get("errors"):
        err = c["errors"]
        out["errors"] = err if level == 0 else {k: v for k, v in err.items() if k != "top"}
    if c.get("alerts"):
        out["alerts"] = c["alerts"]
    if c.get("shared_service"):
        out["shared_service"] = c["shared_service"]
    if level < 2:
        for key in ("depends_on", "clients"):
            if c.get(key):
                out[key] = c[key]
    if c.get("modified_after_creation_s") or c.get("template_changes") or c.get("recent_change"):
        out["changed"] = {
            k: c[k] for k in ("modified_seconds_ago", "modified_after_creation_s", "template_changed_at") if c.get(k)
        }
        if level == 0 and c.get("template_changes"):
            out["changed"]["template"] = c["template_changes"]
    if c.get("max_usage_ratio") and level > 0:
        out["max_usage_ratio"] = c["max_usage_ratio"]
    return out


def render(overview: dict, budget_tokens: int = 4000) -> dict:
    """Compact JSON-able view for the model, components ordered by abnormality, trimmed to `budget_tokens`."""
    scores = component_scores(overview)
    order = sorted(scores, key=lambda cid: (-scores[cid], cid))
    comps = overview.get("components") or {}
    cluster = overview.get("cluster") or {}
    multi_ns = len((overview.get("app") or {}).get("namespaces") or []) > 1
    cluster_view = {k: v for k, v in cluster.items() if v not in (None, [], {})}

    def build(levels: dict[str, int], collapse_quiet: bool, max_changes: int) -> dict:
        """The view at the given per-component detail levels, with quiet components collapsed or not and at most
        `max_changes` recent changes."""
        shown, quiet, collapsed = [], [], []
        for cid in order:
            if collapse_quiet and scores[cid] == 0:
                quiet.append(cid)
            elif levels.get(cid, 0) >= 3:
                collapsed.append(cid)
            else:
                shown.append(_render_component(comps[cid], multi_ns, levels.get(cid, 0)))
        view: dict[str, Any] = {
            "app": overview.get("app", {}).get("name"),
            "namespaces": overview.get("app", {}).get("namespaces"),
        }
        cv = dict(cluster_view)
        if "recent_changes" in cv:
            cv["recent_changes"] = cv["recent_changes"][:max_changes]
        view["cluster"] = cv
        view["components"] = shown
        if collapsed:
            view["components_not_detailed"] = collapsed
        if quiet:
            view["components_without_abnormal_facts"] = quiet
        return view

    levels: dict[str, int] = {}
    view = build(levels, False, MAX_RECENT_CHANGES)
    if estimate_tokens(view) <= budget_tokens:
        return view
    view = build(levels, True, MAX_RECENT_CHANGES)
    # Demote the least abnormal components first: drop detail (1), then to essentials (2), then to a name (3).
    for level in (1, 2, 3):
        for cid in reversed(order):
            if estimate_tokens(view) <= budget_tokens:
                return view
            if scores[cid] == 0:
                continue
            levels[cid] = level
            view = build(levels, True, MAX_RECENT_CHANGES if level < 3 else 4)
    return view
