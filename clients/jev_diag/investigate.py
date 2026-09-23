"""Deep-dive evidence for one component, fetched on demand by the decision tree.

The overview snapshot is deliberately compact. When a component becomes a
hypothesis, code fetches what the overview lacks: the full workload spec with
env values and probes, every event for its pods, recent logs with error lines
split around the latest application change, the content of the ConfigMaps it
references, code-side spec checks (probe ports, env addresses, Service ports),
RBAC bindings for its service account when its logs show denials, and the own
state of the components it calls and of the components whose errors name it.

Everything here is deterministic kubectl reads plus code-side checks.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from clients.jev_diag.collector import (
    ClusterSnapshot,
    compact,
    extract_log_signals,
    is_error_line,
    last_update_time,
    log_line_time,
    parse_time,
    run_kubectl,
    trim,
)
from clients.jev_diag.derive import (
    LOG_CLASSES,
    OBSERVABILITY_RE,
    ROLE_SYNONYMS,
    TELEMETRY_RE,
    _mentions,
    _name_aliases,
)

logger = logging.getLogger("all.jev_diag.investigate")

DETAIL_LOG_TAIL = 300
MAX_ERROR_LINES = 15
MAX_TAIL_LINES = 12
MAX_EVENTS = 15
CONFIG_VALUE_CHARS = 400
CONFIG_TOTAL_CHARS = 4000
_ADDR_RE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://)?(?P<host>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)(?::(?P<port>\d{2,5}))(?![\w.-])"
)
_RBAC_RE = re.compile(
    r"(?i)(is forbidden|\bforbidden\b|\b403\b|cannot (?:get|list|watch|create|update|patch|delete)|RBAC)"
)


def _get_json(args: list[str]) -> dict:
    return json.loads(run_kubectl(args))


def _env_entry(e: dict) -> dict:
    out: dict[str, Any] = {"name": e.get("name")}
    if "value" in e:
        out["value"] = trim(str(e["value"]), 120)
    vf = e.get("valueFrom") or {}
    for key, label in (("secretKeyRef", "secret"), ("configMapKeyRef", "configMap"), ("fieldRef", "field")):
        if vf.get(key):
            ref = vf[key]
            out["from"] = f"{label}:{ref.get('name', ref.get('fieldPath'))}" + (
                f".{ref['key']}" if ref.get("key") else ""
            )
    return out


def _container_detail(c: dict) -> dict:
    return compact(
        {
            "name": c.get("name"),
            "image": c.get("image"),
            "command": c.get("command"),
            "args": [trim(str(a), 160) for a in (c.get("args") or [])][:12],
            "env": [_env_entry(e) for e in (c.get("env") or [])][:40],
            "env_from": [
                (ref.get("configMapRef") or ref.get("secretRef") or {}).get("name") for ref in (c.get("envFrom") or [])
            ],
            "ports": [{"port": p.get("containerPort"), "name": p.get("name")} for p in (c.get("ports") or [])],
            "resources": c.get("resources"),
            "probes": {
                k.replace("Probe", ""): c[k] for k in ("readinessProbe", "livenessProbe", "startupProbe") if c.get(k)
            },
            "volume_mounts": [
                {"name": m.get("name"), "path": m.get("mountPath")} for m in (c.get("volumeMounts") or [])
            ],
        }
    )


def workload_detail(kind: str, name: str, ns: str) -> dict:
    obj = _get_json(["get", kind.lower(), name, "-n", ns, "-o", "json", "--show-managed-fields"])
    spec = obj.get("spec", {})
    template = (
        (spec.get("jobTemplate") or {}).get("spec", {}).get("template", {})
        if kind == "CronJob"
        else spec.get("template", {})
    )
    ps = template.get("spec", {})
    managers = [
        {"manager": f.get("manager"), "op": f.get("operation"), "time": f.get("time")}
        for f in obj.get("metadata", {}).get("managedFields") or []
        if f.get("subresource") != "status"
    ]
    return compact(
        {
            "created": obj.get("metadata", {}).get("creationTimestamp"),
            "last_spec_write": last_update_time(obj.get("metadata", {})),
            "spec_writers": sorted(managers, key=lambda m: m.get("time") or "")[-4:],
            "replicas": spec.get("replicas"),
            "strategy": spec.get("strategy") or spec.get("updateStrategy"),
            "service_account": ps.get("serviceAccountName"),
            "dns_policy": ps.get("dnsPolicy"),
            "dns_config": ps.get("dnsConfig"),
            "host_aliases": ps.get("hostAliases"),
            "host_network": ps.get("hostNetwork"),
            "node_selector": ps.get("nodeSelector"),
            "affinity": trim(json.dumps(ps.get("affinity")), 300) if ps.get("affinity") else None,
            "tolerations": ps.get("tolerations"),
            "init_containers": [_container_detail(c) for c in ps.get("initContainers") or []],
            "containers": [_container_detail(c) for c in ps.get("containers") or []],
            "volumes": [
                compact(
                    {
                        "name": v.get("name"),
                        **{
                            k: v[k]
                            for k in ("configMap", "secret", "persistentVolumeClaim", "emptyDir", "hostPath")
                            if k in v
                        },
                    }
                )
                for v in ps.get("volumes") or []
            ],
            "template_labels": template.get("metadata", {}).get("labels"),
        }
    )


def pod_details(pod_names: list[str], ns: str) -> tuple[list[dict], list[dict]]:
    pods, events = [], []
    try:
        all_events = _get_json(["get", "events", "-n", ns, "-o", "json"]).get("items", [])
    except Exception:  # noqa: BLE001
        all_events = []
    for name in pod_names[:3]:
        try:
            p = _get_json(["get", "pod", name, "-n", ns, "-o", "json"])
        except Exception as exc:  # noqa: BLE001
            pods.append({"name": name, "error": str(exc)[:160]})
            continue
        st = p.get("status", {})
        pods.append(
            compact(
                {
                    "name": name,
                    "node": p.get("spec", {}).get("nodeName"),
                    "phase": st.get("phase"),
                    "started": st.get("startTime"),
                    "conditions": [
                        f"{c.get('type')}={c.get('status')}"
                        + (f" ({c.get('reason')}: {trim(c.get('message'), 120)})" if c.get("status") != "True" else "")
                        for c in st.get("conditions") or []
                    ],
                    "containers": [
                        compact(
                            {
                                "name": cs.get("name"),
                                "ready": cs.get("ready"),
                                "restarts": cs.get("restartCount"),
                                "state": cs.get("state"),
                                "last_state": cs.get("lastState"),
                            }
                        )
                        for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or [])
                    ],
                }
            )
        )
    prefixes = tuple(pod_names) + tuple({n.rsplit("-", 1)[0] for n in pod_names})
    for ev in all_events:
        obj = ev.get("involvedObject") or {}
        if any((obj.get("name") or "").startswith(pfx) for pfx in prefixes):
            events.append(
                {
                    "type": ev.get("type"),
                    "reason": ev.get("reason"),
                    "count": ev.get("count") or 1,
                    "object": f"{(obj.get('kind') or '?').lower()}/{obj.get('name')}",
                    "last": ev.get("lastTimestamp") or ev.get("eventTime"),
                    "message": trim(ev.get("message"), 300),
                }
            )
    events.sort(key=lambda e: e.get("last") or "", reverse=True)
    return pods, events[:MAX_EVENTS]


def log_detail(pod_names: list[str], ns: str, pods: list[dict], change_time: str | None = None) -> dict:
    """Recent logs for up to two pods, with error lines split around the latest application change.

    An error logged before the most recent change to the application cannot be caused by that change; the split
    lets the model separate startup or background errors from the ones that appeared with the fault.
    """
    lines: list[str] = []
    previous: dict[str, list[str]] = {}
    for name in pod_names[:2]:
        try:
            out = run_kubectl(
                [
                    "logs",
                    name,
                    "-n",
                    ns,
                    "--all-containers=true",
                    "--prefix=true",
                    "--timestamps=true",
                    f"--tail={DETAIL_LOG_TAIL}",
                ],
                timeout=40,
            )
            lines += out.splitlines()
        except Exception as exc:  # noqa: BLE001
            lines.append(f"[logs unavailable for {name}: {str(exc)[:120]}]")
        pod = next((p for p in pods if p.get("name") == name), {})
        for cs in pod.get("containers") or []:
            if (cs.get("restarts") or 0) > 0:
                try:
                    prev = run_kubectl(
                        ["logs", name, "-n", ns, "-c", cs["name"], "--previous", "--tail=40"], timeout=30
                    )
                    previous[f"{name}/{cs['name']}"] = [trim(ln, 200) for ln in prev.splitlines()[-MAX_TAIL_LINES:]]
                except Exception:  # noqa: BLE001
                    pass
    all_error_lines = [ln for ln in lines if is_error_line(ln)]
    telemetry_lines = [ln for ln in all_error_lines if TELEMETRY_RE.search(ln)]
    error_lines = [ln for ln in all_error_lines if not TELEMETRY_RE.search(ln)]
    classes: Counter[str] = Counter()
    for ln in error_lines:
        for name, rx in LOG_CLASSES:
            if rx.search(ln):
                classes[name] += 1
    change = parse_time(change_time) if change_time else None
    before_lines: list[str] = []
    after_lines: list[str] = []
    undated_lines: list[str] = []
    for ln in error_lines:
        ts = log_line_time(ln)
        if change is None or ts is None:
            undated_lines.append(ln)
        elif ts < change:
            before_lines.append(ln)
        else:
            after_lines.append(ln)
    before_change, after_change, undated = len(before_lines), len(after_lines), len(undated_lines)
    if change is None:
        samples = extract_log_signals(error_lines, limit=MAX_ERROR_LINES)
    else:
        # errors that appeared after the change come first; each sample carries its side of the change
        samples = [
            {**smp, "before_latest_change": False} for smp in extract_log_signals(after_lines, limit=MAX_ERROR_LINES)
        ]
        room = max(0, MAX_ERROR_LINES - len(samples))
        samples += [{**smp, "before_latest_change": True} for smp in extract_log_signals(before_lines, limit=room)]
        samples += extract_log_signals(undated_lines, limit=max(0, MAX_ERROR_LINES - len(samples)))
    return compact(
        {
            "total_lines": len(lines),
            "error_lines": len(error_lines),
            "latest_application_change": change_time,
            "error_lines_before_latest_change": before_change if change is not None else None,
            "error_lines_after_latest_change": after_change if change is not None else None,
            "error_lines_without_timestamp": undated or None,
            "error_classes_after_latest_change": dict(
                Counter(name for ln in after_lines for name, rx in LOG_CLASSES if rx.search(ln))
            )
            if change is not None
            else None,
            "telemetry_export_error_lines": len(telemetry_lines),
            "error_classes": dict(classes),
            "error_samples": samples,
            "telemetry_error_samples": extract_log_signals(telemetry_lines, limit=3),
            "tail": [trim(re.sub(r"^\[pod/[^\]]+\]\s?", "", ln), 200) for ln in lines[-MAX_TAIL_LINES:]],
            "previous_container_logs": previous,
        }
    )


def configmap_detail(names: list[str], ns: str) -> dict:
    out: dict[str, Any] = {}
    budget = CONFIG_TOTAL_CHARS
    for name in names[:6]:
        try:
            cm = _get_json(["get", "configmap", name, "-n", ns, "-o", "json", "--show-managed-fields"])
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)[:120]}
            continue
        data = cm.get("data") or {}
        excerpt = {}
        for key, value in data.items():
            take = min(CONFIG_VALUE_CHARS, max(0, budget))
            excerpt[key] = trim(str(value).replace("\n", " ⏎ "), take) if take else "…"
            budget -= len(excerpt[key])
        out[name] = {"last_update": last_update_time(cm.get("metadata", {})), "keys": sorted(data), "data": excerpt}
    return out


def service_checks(comp: dict, workload: dict, pods: list[dict] | None = None) -> list[str]:
    """Service-to-workload port comparisons.

    A targetPort that no container declares only matters for traffic to a Running pod; while every pod is
    Pending or terminating the symptom is scheduling, not routing, so the comparison is skipped.
    """
    ports = {p.get("port") for c in workload.get("containers", []) for p in c.get("ports", []) if p.get("port")}
    names = {p.get("name") for c in workload.get("containers", []) for p in c.get("ports", []) if p.get("name")}
    running = pods is None or any(p.get("phase") == "Running" for p in pods)
    checks = []
    for svc in comp.get("services") or []:
        for spec in svc.get("ports") or []:
            # format "80->8080/TCP" or "80->http/TCP"
            target = spec.split("->")[1].split("/")[0] if "->" in spec else None
            if target is None or not ports or not running:
                continue
            if target.isdigit() and int(target) not in ports:
                checks.append(
                    f"service {svc['name']} targetPort {target} is not exposed by any container port {sorted(ports)}"
                )
            elif not target.isdigit() and target not in names:
                checks.append(
                    f"service {svc['name']} targetPort name {target!r} matches no container port name {sorted(names)}"
                )
        if svc.get("ready_endpoints") == 0:
            checks.append(f"service {svc['name']} has no ready endpoints")
    return checks


def _service_ports(comp: dict) -> dict[str, set[int]]:
    """Service name -> front ports, for every Service that selects the component."""
    out: dict[str, set[int]] = {}
    for svc in comp.get("services") or []:
        ports = set()
        for spec in svc.get("ports") or []:
            front = spec.split("->")[0]
            if front.isdigit():
                ports.add(int(front))
        out[svc["name"]] = ports
    return out


def spec_checks(snapshot: ClusterSnapshot, cid: str, workload: dict, pods: list[dict] | None = None) -> list[str]:
    """Deterministic comparisons inside the candidate's own spec and against the Services it addresses.

    Probe ports are compared with the container's declared ports, but only for a container that is currently
    not ready somewhere: a Ready container answers its probe, so an undeclared probe port is not a defect. Env
    values of the form host:port are compared with the ports of the Service they name. Each mismatch is an
    observation for the model to weigh.
    """
    checks: list[str] = []
    not_ready = {cs.get("name") for pod in pods or [] for cs in pod.get("containers") or [] if cs.get("ready") is False}
    services_by_name: dict[str, tuple[str, set[int]]] = {}
    for other, comp in snapshot.components.items():
        for name, ports in _service_ports(comp).items():
            services_by_name[name] = (other, ports)
    for c in workload.get("containers", []):
        declared = {p.get("port") for p in c.get("ports") or [] if p.get("port")}
        for kind, probe in (c.get("probes") or {}).items():
            handler = probe.get("httpGet") or probe.get("tcpSocket") or probe.get("grpc") or {}
            port = handler.get("port")
            if isinstance(port, int) and declared and port not in declared and c.get("name") in not_ready:
                checks.append(
                    f"container {c['name']} {kind} probe targets port {port}; the container declares ports "
                    f"{sorted(declared)} and is not ready"
                )
        for e in c.get("env") or []:
            value = str(e.get("value") or "")
            if not value or "$(" in value:
                continue
            for m in _ADDR_RE.finditer(value):
                host, port = m.group("host").lower(), int(m.group("port"))
                if host in ("localhost", "0.0.0.0") or host not in services_by_name:
                    continue
                target, ports = services_by_name[host]
                if target == cid or not ports:
                    continue
                if port not in ports:
                    checks.append(
                        f"env {e['name']}={value!r} addresses Service {host} on port {port}; that Service "
                        f"exposes ports {sorted(ports)} (component {target})"
                    )
    return checks


def rbac_detail(sa: str | None, ns: str) -> dict:
    sa = sa or "default"
    out: dict[str, Any] = {"service_account": f"{ns}/{sa}"}
    try:
        rb = _get_json(["get", "rolebindings,clusterrolebindings", "-A", "-o", "json"]).get("items", [])
        bound = []
        for b in rb:
            for s in b.get("subjects") or []:
                if s.get("kind") == "ServiceAccount" and s.get("name") == sa and (s.get("namespace") or ns) == ns:
                    bound.append(
                        f"{b.get('kind')}/{b.get('metadata', {}).get('name')} -> {b.get('roleRef', {}).get('kind')}/{b.get('roleRef', {}).get('name')}"
                    )
        out["bindings"] = bound[:10]
    except Exception as exc:  # noqa: BLE001
        out["bindings_error"] = str(exc)[:120]
    try:
        can = run_kubectl(["auth", "can-i", "--list", f"--as=system:serviceaccount:{ns}:{sa}", "-n", ns], timeout=30)
        out["can_i"] = [trim(ln, 160) for ln in can.splitlines()[1:14]]
    except Exception as exc:  # noqa: BLE001
        out["can_i_error"] = str(exc)[:120]
    return out


def _role_tokens(snapshot: ClusterSnapshot, cid: str) -> set[str]:
    """Words that identify a dependency in another component's error text: name parts and image repo names."""
    comp = snapshot.components[cid]
    tokens = {t for t in re.split(r"[-_./]", comp["name"].lower()) if len(t) >= 4}
    for c in comp.get("containers") or []:
        image = (c.get("image") or "").split("@")[0].split(":")[0]
        for part in image.split("/")[-2:]:
            tokens |= {t for t in re.split(r"[-_.]", part.lower()) if len(t) >= 4}
    tokens -= {"deployment", "service", "server", "latest", "library", "docker"}
    for group in ROLE_SYNONYMS:
        if tokens & group:
            tokens |= group
    return tokens


def failing_targets(snapshot: ClusterSnapshot, cid: str, calls: set[str], error_samples: list[dict]) -> list[str]:
    """Dependencies of `cid` that its own error lines refer to, by name part or image name (code-side matching)."""
    text = " ".join(s.get("line", "") for s in error_samples if not TELEMETRY_RE.search(s.get("line", ""))).lower()
    if not text:
        return []
    hits = []
    for dep in sorted(calls):
        if dep not in snapshot.components or dep == cid or OBSERVABILITY_RE.search(snapshot.components[dep]["name"]):
            continue
        if any(re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])", text) for tok in _role_tokens(snapshot, dep)):
            hits.append(dep)
    return hits


def dependency_state(snapshot: ClusterSnapshot, dep: str, exclude: str) -> dict:
    """A dependency's condition judged from its own state, not from the errors `exclude` logs about it."""
    comp = snapshot.components[dep]
    own_signals = [s for s in comp.get("signals") or [] if not s.startswith("errors logged by")]
    pods = comp.get("pods") or []
    ready = sum(1 for p in pods if p.get("ready") and p["ready"].split("/")[0] == p["ready"].split("/")[1] != "0")
    restarts = sum(int(p.get("restarts") or 0) for p in pods)
    kinds = [k for k in comp.get("evidence_kinds") or [] if k != "pointed_at"]
    return compact(
        {
            "kind": comp.get("kind"),
            "pods_ready": f"{ready}/{len(pods)}",
            "restarts": restarts,
            "own_evidence_kinds": kinds,
            "own_signals": own_signals[:4],
            "own_error_log_lines": comp.get("log_error_lines"),
            "last_spec_change": comp.get("modified") if comp.get("spec_changed_after_creation_s") else None,
            "looks_healthy_on_its_own": not own_signals and not kinds and ready == len(pods) and restarts == 0,
        }
    )


def latest_application_change(snapshot: ClusterSnapshot) -> str | None:
    """Timestamp of the most recent change to an application (non-telemetry) object, if any was recorded."""
    for entry in snapshot.cluster.get("recent_changes") or []:
        if entry.get("role") != "observability" and entry.get("modified"):
            return entry["modified"]
    return None


def collect_component_detail(snapshot: ClusterSnapshot, cid: str) -> dict:
    """Everything the tree wants to know about one component before judging it."""
    comp = snapshot.components[cid]
    ns, kind, name = comp["namespace"], comp["kind"], comp["name"]
    errors: list[str] = []
    detail: dict[str, Any] = {"component": cid, "namespace": ns}
    try:
        detail["workload"] = workload_detail(kind, name, ns)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"workload: {exc}")
        detail["workload"] = {}
    pod_names = [p["name"] for p in comp.get("pods") or [] if p.get("name")]
    pods, events = pod_details(pod_names, ns)
    detail["pods"], detail["events"] = pods, events
    detail["logs"] = log_detail(
        [p["name"] for p in pods if not p.get("error")], ns, pods, latest_application_change(snapshot)
    )
    detail["spec_checks"] = spec_checks(snapshot, cid, detail["workload"], pods)
    refs = comp.get("config_refs") or {}
    if refs.get("configmaps"):
        detail["configmaps"] = configmap_detail(refs["configmaps"], ns)
    if comp.get("config_objects"):
        detail["secrets"] = {k: v for k, v in comp["config_objects"].items() if k.startswith("secret/")}
    detail["service_checks"] = service_checks(comp, detail["workload"], pods)
    logs_text = " ".join(s.get("line", "") for s in detail["logs"].get("error_samples", []))
    sa = detail["workload"].get("service_account")
    roles = [r for r in snapshot.cluster.get("rbac") or [] if cid in (r.get("components") or [])]
    if _RBAC_RE.search(logs_text) or (sa and sa != "default") or roles:
        detail["rbac"] = rbac_detail(sa, ns)
        detail["rbac"]["roles"] = roles
    # dependency edges: env values naming other components, plus the error pointers computed earlier
    aliases = _name_aliases(snapshot.components)
    calls = set(comp.get("errors_point_to") or [])
    for c in detail["workload"].get("containers", []):
        for e in c.get("env", []):
            if e.get("value"):
                calls |= _mentions(str(e["value"]), aliases, exclude=cid)
    detail["calls"] = {
        other: dependency_state(snapshot, other, cid) for other in sorted(calls) if other in snapshot.components
    }
    detail["called_by_with_errors"] = comp.get("referenced_by_errors_from") or {}
    detail["failing_targets"] = failing_targets(snapshot, cid, calls, detail["logs"].get("error_samples") or [])
    if errors:
        detail["collection_errors"] = errors
    return compact(detail)
