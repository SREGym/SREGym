"""Deep-dive evidence for one component, fetched on demand by the decision tree.

The overview snapshot is deliberately compact. When a component becomes a
hypothesis, code fetches what the overview lacks: the full workload spec with
env values and probes, the current events for its pods, recent logs with each
error signature marked as still occurring or stopped, the content of the
ConfigMaps it references, code-side spec checks (probe targets, env addresses,
Service ports and traffic policy), RBAC bindings for its service account when
its logs show denials, and the own state of the components it calls and of the
components whose errors name it.

Everything here is deterministic kubectl reads plus code-side checks.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from clients.jev_diag.checks import describe_probe
from clients.jev_diag.collector import (
    WARMUP_SECONDS,
    ClusterSnapshot,
    compact,
    extract_log_signals,
    is_error_line,
    last_update_time,
    log_line_time,
    parse_time,
    run_kubectl,
    strip_ansi,
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
from clients.jev_diag.timeutil import now

logger = logging.getLogger("all.jev_diag.investigate")

DETAIL_LOG_TAIL = 300
MAX_ERROR_LINES = 15
MAX_TAIL_LINES = 12
MAX_EVENTS = 15
CONFIG_VALUE_CHARS = 400
CONFIG_TOTAL_CHARS = 4000
MAX_REPEATED = 4
MIN_REPEATS = 3
MIN_REPEAT_SPAN_SECONDS = 30
_ADDR_RE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://)?(?P<host>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)(?::(?P<port>\d{2,5}))(?![\w.-])"
)
_RBAC_RE = re.compile(
    r"(?i)(is forbidden|\bforbidden\b|\b403\b|cannot (?:get|list|watch|create|update|patch|delete)|RBAC)"
)
_PREFIX_RE = re.compile(r"^\[pod/[^/\]]+/([^\]]+)\]\s?")
_TS_PREFIX_RE = re.compile(r"^\S*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\S*\s")
_VALUE_TOKEN_RE = re.compile(r"[\w./:=-]*\d[\w./:=-]*")
_INPUT_VALUE_RE = re.compile(r"^[A-Za-z][\w.-]{1,62}$")
_NOT_INPUT_VALUES = {"true", "false", "yes", "no", "on", "off", "none", "null", "info", "debug", "warn", "error"}


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
            "args": [trim(str(a), 400) for a in (c.get("args") or [])][:12],
            "env": [_env_entry(e) for e in (c.get("env") or [])][:40],
            "env_from": [
                (ref.get("configMapRef") or ref.get("secretRef") or {}).get("name") for ref in (c.get("envFrom") or [])
            ],
            "ports": [{"port": p.get("containerPort"), "name": p.get("name")} for p in (c.get("ports") or [])],
            "resources": c.get("resources"),
            "restart_policy": c.get("restartPolicy"),
            "probes": {
                k.replace("Probe", ""): c[k] for k in ("readinessProbe", "livenessProbe", "startupProbe") if c.get(k)
            },
            "volume_mounts": [
                compact({"name": m.get("name"), "path": m.get("mountPath"), "sub_path": m.get("subPath")})
                for m in (c.get("volumeMounts") or [])
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
        if f.get("subresource") not in ("status", "scale")
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
            "restart_policy": ps.get("restartPolicy"),
            "init_containers": [_container_detail(c) for c in ps.get("initContainers") or []],
            "containers": [_container_detail(c) for c in ps.get("containers") or []],
            "volumes": [
                compact(
                    {
                        "name": v.get("name"),
                        **{
                            k: v[k]
                            for k in (
                                "configMap",
                                "secret",
                                "persistentVolumeClaim",
                                "emptyDir",
                                "hostPath",
                                "projected",
                            )
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
    """Pod state for up to three pods, and their warning events that are still current.

    A warning event whose last occurrence precedes the moment its pod became Ready (the pod is Ready now) is
    start-up history, as in the overview, and is left out.
    """
    pods, events = [], []
    try:
        all_events = _get_json(["get", "events", "-n", ns, "-o", "json"]).get("items", [])
    except Exception:  # noqa: BLE001
        all_events = []
    ready_since: dict[str, Any] = {}
    for name in pod_names[:3]:
        try:
            p = _get_json(["get", "pod", name, "-n", ns, "-o", "json"])
        except Exception as exc:  # noqa: BLE001
            pods.append({"name": name, "error": str(exc)[:160]})
            continue
        st = p.get("status", {})
        ready = next(
            (
                c.get("lastTransitionTime")
                for c in st.get("conditions") or []
                if c.get("type") == "Ready" and c.get("status") == "True"
            ),
            None,
        )
        ready_since[name] = parse_time(ready)
        pods.append(
            compact(
                {
                    "name": name,
                    "node": p.get("spec", {}).get("nodeName"),
                    "phase": st.get("phase"),
                    "started": st.get("startTime"),
                    "ready_since": ready,
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
        if not any((obj.get("name") or "").startswith(pfx) for pfx in prefixes):
            continue
        last = ev.get("lastTimestamp") or ev.get("eventTime")
        became_ready = ready_since.get(obj.get("name") or "")
        if ev.get("type") == "Warning" and became_ready and (parse_time(last) or became_ready) <= became_ready:
            continue
        events.append(
            {
                "type": ev.get("type"),
                "reason": ev.get("reason"),
                "count": ev.get("count") or 1,
                "object": f"{(obj.get('kind') or '?').lower()}/{obj.get('name')}",
                "last": last,
                "message": trim(ev.get("message"), 300),
            }
        )
    events.sort(key=lambda e: e.get("last") or "", reverse=True)
    return pods, events[:MAX_EVENTS]


def _settled_restart(pod: dict, cs: dict) -> bool:
    """A container that crashed only during warm-up and has been ready and running steadily since.

    Its previous run's logs are start-up history (a dependency that was not up yet), not evidence of the fault.
    """
    if not cs.get("ready"):
        return False
    started = parse_time(pod.get("started"))
    finished = parse_time(((cs.get("last_state") or {}).get("terminated") or {}).get("finishedAt"))
    running = parse_time(((cs.get("state") or {}).get("running") or {}).get("startedAt"))
    if not (started and finished and running):
        return False
    return (finished - started).total_seconds() <= WARMUP_SECONDS and (now() - running).total_seconds() >= 60


def _crash_looping(pods: list[dict]) -> set[str]:
    out = set()
    for pod in pods:
        for cs in pod.get("containers") or []:
            waiting = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
            if waiting == "CrashLoopBackOff" or ((cs.get("restarts") or 0) > 0 and not cs.get("ready")):
                out.add(cs.get("name"))
    return out


def repeated_failures(lines: list[str]) -> list[dict]:
    """Error lines that recur with the same wording, and the value tokens that never change between repeats.

    Lines are grouped after replacing every token that contains a digit. For a group seen at least MIN_REPEATS
    times over at least MIN_REPEAT_SPAN_SECONDS, the tokens identical in every occurrence (an id, an offset, a
    position, a code location) are reported. Code does not interpret them; the model judges what they mean.
    """
    groups: dict[str, list[tuple[Any, list[str], str]]] = {}
    for line in lines:
        text = _TS_PREFIX_RE.sub("", _PREFIX_RE.sub("", line, count=1), count=1).strip()
        tokens = _VALUE_TOKEN_RE.findall(text)
        key = _VALUE_TOKEN_RE.sub("<v>", text)
        groups.setdefault(key, []).append((log_line_time(line), tokens, text))
    out = []
    for occurrences in groups.values():
        if len(occurrences) < MIN_REPEATS:
            continue
        times = sorted(t for t, _, _ in occurrences if t)
        span = int((times[-1] - times[0]).total_seconds()) if len(times) > 1 else 0
        if span < MIN_REPEAT_SPAN_SECONDS:
            continue
        width = min(len(tokens) for _, tokens, _ in occurrences)
        constant = [occurrences[0][1][i] for i in range(width) if len({tokens[i] for _, tokens, _ in occurrences}) == 1]
        if constant:
            out.append(
                {
                    "line": trim(occurrences[-1][2], 200),
                    "count": len(occurrences),
                    "span_seconds": span,
                    "constant_tokens": constant[:4],
                }
            )
    out.sort(key=lambda r: -r["count"])
    return out[:MAX_REPEATED]


def log_detail(pod_names: list[str], ns: str, pods: list[dict], change_time: str | None = None) -> dict:
    """Recent logs for up to two pods; each error signature is marked as still occurring or stopped.

    A signature is stopped when it has been silent for much longer than its own usual gap (see
    collector.signature_recency); errors of a crash-looping container count as occurring. `change_time` is
    accepted for compatibility and ignored.
    """
    del change_time
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
            lines += [strip_ansi(ln) for ln in out.splitlines()]
        except Exception as exc:  # noqa: BLE001
            lines.append(f"[logs unavailable for {name}: {str(exc)[:120]}]")
        pod = next((p for p in pods if p.get("name") == name), {})
        for cs in pod.get("containers") or []:
            if (cs.get("restarts") or 0) > 0 and not _settled_restart(pod, cs):
                try:
                    prev = run_kubectl(
                        ["logs", name, "-n", ns, "-c", cs["name"], "--previous", "--tail=40"], timeout=30
                    )
                    previous[f"{name}/{cs['name']}"] = [
                        trim(strip_ansi(ln), 200) for ln in prev.splitlines()[-MAX_TAIL_LINES:]
                    ]
                except Exception:  # noqa: BLE001
                    pass
    all_error_lines = [ln for ln in lines if is_error_line(ln)]
    telemetry_lines = [ln for ln in all_error_lines if TELEMETRY_RE.search(ln)]
    error_lines = [ln for ln in all_error_lines if not TELEMETRY_RE.search(ln)]
    samples = extract_log_signals(error_lines, limit=MAX_ERROR_LINES, ongoing_containers=_crash_looping(pods))
    ongoing = [s for s in samples if s.get("state") != "stopped"]
    stopped = [s for s in samples if s.get("state") == "stopped"]
    classes: Counter[str] = Counter()
    for smp in ongoing:
        for name, rx in LOG_CLASSES:
            if rx.search(smp.get("line") or ""):
                classes[name] += int(smp.get("count") or 1)
    return compact(
        {
            "total_lines": len(lines),
            "error_lines": len(error_lines),
            "error_lines_still_occurring": sum(int(s.get("count") or 1) for s in ongoing),
            "error_lines_stopped": sum(int(s.get("count") or 1) for s in stopped),
            "telemetry_export_error_lines": len(telemetry_lines),
            "error_classes_still_occurring": dict(classes),
            "error_samples": samples,
            "repeated_failures": repeated_failures(error_lines),
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


def service_checks(
    comp: dict, workload: dict, pods: list[dict] | None = None, snapshot: ClusterSnapshot | None = None
) -> list[str]:
    """Service-to-workload comparisons: ports, endpoints, traffic policy, and selectors spanning workloads.

    A targetPort that no container declares only matters for traffic to a Running pod; while every pod is
    Pending or terminating the symptom is scheduling, not routing, so the comparison is skipped.
    """
    ports = {p.get("port") for c in workload.get("containers", []) for p in c.get("ports", []) if p.get("port")}
    names = {p.get("name") for c in workload.get("containers", []) for p in c.get("ports", []) if p.get("name")}
    running = pods is None or any(p.get("phase") == "Running" for p in pods)
    checks = []
    for svc in comp.get("services") or []:
        targets = [spec.split("->")[1].split("/")[0] for spec in svc.get("ports") or [] if "->" in spec]
        for target in targets:
            if not ports or not running:
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
        for gap in svc.get("local_policy_gaps") or []:
            checks.append(
                f"service {svc['name']} has internalTrafficPolicy=Local and ready endpoints only on "
                f"{', '.join(n.split('.')[0] for n in svc.get('endpoint_nodes') or []) or 'no node'}; client {gap}, "
                "where it has no local endpoint"
            )
        if snapshot is not None and len(svc.get("selects_multiple_workloads") or []) > 1:
            for other in svc["selects_multiple_workloads"]:
                other_comp = snapshot.components.get(other) or {}
                other_ports = {p for c in other_comp.get("containers") or [] for p in c.get("ports") or []}
                for target in targets:
                    if target.isdigit() and other_ports and int(target) not in other_ports:
                        checks.append(
                            f"service {svc['name']} (selector {svc.get('selector') or '?'}) also selects pods of "
                            f"{other}, which expose {sorted(other_ports)} but not targetPort {target}"
                        )
    return list(dict.fromkeys(checks))


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
    not ready somewhere: a Ready container answers its probe, so an undeclared probe port is not a defect. The
    whole probe target (scheme, path, port) is reported. Env values of the form host:port are compared with the
    ports of the Service they name. Each mismatch is an observation for the model to weigh.
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
                    f"container {c['name']} {kind} probe ({describe_probe(probe)}) targets port {port}; the "
                    f"container declares ports {sorted(declared)} and is not ready"
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
    """Dependencies of `cid` that its still-occurring error lines refer to, by name part or image name."""
    text = " ".join(
        s.get("line", "")
        for s in error_samples
        if s.get("state") != "stopped" and not TELEMETRY_RE.search(s.get("line", ""))
    ).lower()
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
    own_signals = [
        s for s in comp.get("signals") or [] if not s.startswith(("errors logged by", "server ", "rejects logins"))
    ]
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


def input_candidates(workload: dict) -> list[dict]:
    """Env values that could name an input source (a queue, topic, stream, or table): short identifier-like values.

    The model selects among them; code never guesses from variable names.
    """
    out = []
    for c in workload.get("containers") or []:
        for e in c.get("env") or []:
            value = str(e.get("value") or "")
            if _INPUT_VALUE_RE.match(value) and value.lower() not in _NOT_INPUT_VALUES and not value.isdigit():
                out.append({"container": c.get("name"), "env": e.get("name"), "value": value})
    return out[:16]


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
    detail["logs"] = log_detail([p["name"] for p in pods if not p.get("error")], ns, pods)
    detail["spec_checks"] = spec_checks(snapshot, cid, detail["workload"], pods)
    for key in ("template_changes", "admission_changes", "calls_services", "clients", "blocked_input"):
        if comp.get(key):
            detail[key] = comp[key]
    refs = comp.get("config_refs") or {}
    if refs.get("configmaps"):
        detail["configmaps"] = configmap_detail(refs["configmaps"], ns)
    if comp.get("config_objects"):
        detail["secrets"] = {k: v for k, v in comp["config_objects"].items() if k.startswith("secret/")}
    detail["service_checks"] = service_checks({**comp, "id": cid}, detail["workload"], pods, snapshot)
    logs_text = " ".join(s.get("line", "") for s in detail["logs"].get("error_samples", []))
    sa = detail["workload"].get("service_account")
    roles = [r for r in snapshot.cluster.get("rbac") or [] if cid in (r.get("components") or [])]
    if _RBAC_RE.search(logs_text) or (sa and sa != "default") or roles:
        detail["rbac"] = rbac_detail(sa, ns)
        detail["rbac"]["roles"] = roles
    candidates = input_candidates(detail["workload"])
    if candidates:
        detail["input_candidates"] = candidates
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
