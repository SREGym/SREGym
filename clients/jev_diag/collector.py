"""Deterministic Kubernetes state collection for the Jev diagnosis agent.

Everything in this module is ordinary code: kubectl reads, label matching, and
anomaly signals computed from the returned JSON. No model is involved. The
result is a compact, JSON-serialisable snapshot keyed by component id
("deployment/frontend") so a Jev question can point at
`components["deployment/frontend"]`.

Design rules (see docs.typesafe.ai/model-jaggedness): keep arithmetic and
counting in code, send the model only the fields a judgment needs, and keep
the state under the model's context budget.
"""

from __future__ import annotations

import ast
import copy
import json
import logging
import re
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("all.jev_diag.collector")

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job")
KUBECTL_TIMEOUT = 45
LOG_TIMEOUT = 25
DEFAULT_LOG_TAIL = 200
MAX_LOG_PODS_PER_COMPONENT = 2
MAX_LOG_SIGNALS = 8
MAX_PREVIOUS_LOG_LINES = 15
MAX_EVENTS_PER_COMPONENT = 6
# Error lines logged this soon after a pod started are warm-up noise (dependencies not up yet), not evidence.
WARMUP_SECONDS = 120
MAX_MESSAGE_CHARS = 240

# Normal states that carry no signal on their own.
_BENIGN_WAITING_REASONS = {"ContainerCreating", "PodInitializing"}
_PRESSURE_CONDITIONS = {"MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"}

_ERROR_LINE_RE = re.compile(
    r"(?i)(\berror\b|\berr\b|\bfail(?:ed|ure|ing)?\b|exception|panic|fatal|traceback|refused|timed?\s?out|"
    r"\btimeout\b|unavailable|denied|unauthorized|forbidden|not found|no such host|could not resolve|"
    r"\bEOF\b|\bOOM\b|out of memory|killed|crash|unhealthy|\b5\d\d\b|deadline exceeded|reset by peer|"
    r"broken pipe|unreachable|no route to host|throttl)"
)
_BENIGN_LINE_RE = re.compile(r"(?i)\b(errors?[:=]\s*0|0 errors|failed[:=]\s*0|no errors?)\b")
# Structured log lines that declare a non-error level are not evidence, whatever words they contain.
_INFO_LEVEL_RE = re.compile(
    r'(?i)(\blevel=(?:info|debug|trace)\b|"(?:level|severity)"\s*:\s*"(?:info|debug|trace)"|\b(?:INFO|DEBUG|TRACE)\b)'
)
_TIMESTAMP_RE = re.compile(r"^\[?\S*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s\]]*\]?\s*")
_RFC3339_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))")
_PREFIX_RE = re.compile(r"^\[pod/[^/\]]+/([^\]]+)\]\s?")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ADDR_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_NUM_RE = re.compile(r"\d{3,}")
_WS_RE = re.compile(r"\s+")


@dataclass
class ClusterSnapshot:
    """Everything the collector learned, before any budget trimming."""

    app: dict
    components: dict[str, dict]
    cluster: dict
    errors: list[str] = field(default_factory=list)
    collected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def to_state(self) -> dict:
        """The JSON object handed to Jev as `state` (before budget fitting)."""
        return {"application": self.app, "cluster": self.cluster, "components": self.components}


# --------------------------------------------------------------------------- kubectl


_KUBECTL_OBSERVER: Callable[[dict], None] | None = None


def set_kubectl_observer(observer: Callable[[dict], None] | None) -> None:
    """Register a callback that receives one record per kubectl invocation (for decision tracing)."""
    global _KUBECTL_OBSERVER
    _KUBECTL_OBSERVER = observer


def run_kubectl(args: list[str], *, timeout: int = KUBECTL_TIMEOUT) -> str:
    """Run kubectl and return stdout. Raises RuntimeError with stderr on failure."""
    started = time.monotonic()
    record = {"cmd": "kubectl " + " ".join(args), "returncode": None, "stdout_bytes": 0, "error": None}
    try:
        proc = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)
        record["returncode"] = proc.returncode
        record["stdout_bytes"] = len(proc.stdout)
        if proc.returncode != 0:
            record["error"] = proc.stderr.strip()[:400]
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {record['error']}")
        return proc.stdout
    except subprocess.TimeoutExpired:
        record["error"] = f"timed out after {timeout}s"
        raise
    finally:
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        if _KUBECTL_OBSERVER is not None:
            _KUBECTL_OBSERVER(record)


def kubectl_items(
    resource: str, namespace: str | None, errors: list[str], *, cluster_scoped: bool = False
) -> list[dict]:
    """`kubectl get <resource> -o json` items, or [] with the failure recorded."""
    args = ["get", resource, "-o", "json", "--show-managed-fields"]
    if not cluster_scoped:
        args += ["-n", namespace] if namespace else ["-A"]
    try:
        return json.loads(run_kubectl(args)).get("items", [])
    except Exception as exc:  # noqa: BLE001 - one failed read must not abort the snapshot
        errors.append(str(exc))
        logger.warning("Skipping %s in %s: %s", resource, namespace or "all namespaces", exc)
        return []


# --------------------------------------------------------------------------- helpers


def trim(text: str | None, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = _WS_RE.sub(" ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def selector_matches(selector: dict | None, labels: dict | None) -> bool:
    """True when every selector key/value is present in labels. Empty selector matches nothing here."""
    if not selector:
        return False
    labels = labels or {}
    return all(labels.get(k) == v for k, v in selector.items())


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


def compact(obj):
    """Recursively drop None values and empty lists/dicts so the model only sees fields that carry information."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v = compact(v)
            if v is None or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        return [compact(v) for v in obj]
    return obj


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def last_update_time(meta: dict) -> str | None:
    """Most recent spec write to an object, from managedFields (status-subresource writes excluded)."""
    times = [
        f.get("time") for f in meta.get("managedFields") or [] if f.get("time") and f.get("subresource") != "status"
    ]
    return max(times) if times else meta.get("creationTimestamp")


CHANGE_GRACE_SECONDS = 60  # writes this soon after the application deployed are still part of the rollout


def deployment_time(components: dict[str, dict]) -> datetime | None:
    """When the application was deployed: the median creation time of its workloads."""
    times = sorted(t for t in (parse_time(c.get("created")) for c in components.values()) if t)
    return times[len(times) // 2] if times else None


def change_after_deploy(modified: str | None, deploy_time: datetime | None) -> int | None:
    """Seconds between the application deploy and this object's last spec write, if beyond the grace period."""
    mod = parse_time(modified)
    if mod is None or deploy_time is None:
        return None
    delta = int((mod - deploy_time).total_seconds())
    return delta if delta > CHANGE_GRACE_SECONDS else None


def _probe_summary(probe: dict | None) -> dict | None:
    if not probe:
        return None
    if "httpGet" in probe:
        g = probe["httpGet"]
        return {"http": f"{g.get('path', '/')}:{g.get('port')}"}
    if "tcpSocket" in probe:
        return {"tcp": probe["tcpSocket"].get("port")}
    if "grpc" in probe:
        return {"grpc": probe["grpc"].get("port")}
    if "exec" in probe:
        return {"exec": " ".join(probe["exec"].get("command") or [])[:80]}
    return {"other": True}


def _probe_port(probe: dict | None):
    if not probe:
        return None
    for key in ("httpGet", "tcpSocket", "grpc"):
        if key in probe:
            return probe[key].get("port")
    return None


def component_id(kind: str, name: str) -> str:
    return f"{kind.lower()}/{name}"


def _strip_hash_suffix(name: str, segments: int) -> str:
    parts = name.split("-")
    return "-".join(parts[:-segments]) if len(parts) > segments else name


# --------------------------------------------------------------------------- workloads


def summarize_workload(obj: dict) -> dict:
    """Compact, model-facing description of a Deployment/StatefulSet/DaemonSet/CronJob/Job."""
    kind = obj.get("kind", "")
    meta = obj.get("metadata", {})
    spec = obj.get("spec", {})
    status = obj.get("status", {})
    if kind == "CronJob":
        template = (spec.get("jobTemplate") or {}).get("spec", {}).get("template", {})
    else:
        template = spec.get("template", {})
    pod_spec = template.get("spec", {})
    signals: list[str] = []
    spec_flags: list[str] = []
    extra: dict = {}

    if kind == "DaemonSet":
        replicas = {
            "desired": status.get("desiredNumberScheduled", 0),
            "ready": status.get("numberReady", 0),
            "available": status.get("numberAvailable", 0),
        }
    elif kind == "CronJob":
        active = len(status.get("active") or [])
        replicas = {"desired": 0, "ready": 0}
        extra["cronjob"] = {
            "schedule": spec.get("schedule"),
            "suspend": bool(spec.get("suspend")),
            "concurrency_policy": spec.get("concurrencyPolicy"),
            "active_jobs": active,
            "last_schedule": status.get("lastScheduleTime"),
            "last_successful": status.get("lastSuccessfulTime"),
        }
        extra["jobs"] = []
        if spec.get("suspend"):
            signals.append("cronjob is suspended")
        last_sched, last_ok = parse_time(status.get("lastScheduleTime")), parse_time(status.get("lastSuccessfulTime"))
        if active and (last_ok is None or (last_sched and last_ok < last_sched)):
            signals.append(
                f"{active} job(s) still active and no successful completion since the last schedule "
                f"at {status.get('lastScheduleTime')} (last success: {status.get('lastSuccessfulTime') or 'never'})"
            )
    elif kind == "Job":
        replicas = {
            "desired": spec.get("completions", 1) or 1,
            "ready": status.get("succeeded", 0) or 0,
        }
        extra["job"] = {
            "active": status.get("active", 0),
            "succeeded": status.get("succeeded", 0),
            "failed": status.get("failed", 0),
            "start_time": status.get("startTime"),
            "completion_time": status.get("completionTime"),
        }
        for cond in status.get("conditions") or []:
            if cond.get("type") == "Failed" and cond.get("status") == "True":
                signals.append(f"job failed ({cond.get('reason')}): {trim(cond.get('message'), 160)}")
        if status.get("active") and not status.get("completionTime"):
            signals.append(f"job has {status.get('active')} active pod(s) and has not completed")
    else:
        replicas = {
            "desired": spec.get("replicas", 1),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", status.get("currentReplicas", 0)),
            "updated": status.get("updatedReplicas", 0),
        }

    strategy = spec.get("strategy") or spec.get("updateStrategy")
    if strategy:
        rolling = strategy.get("rollingUpdate") or {}
        extra["strategy"] = compact({"type": strategy.get("type"), **rolling})
        desired = replicas.get("desired") or 0
        max_unavail, max_surge = rolling.get("maxUnavailable"), rolling.get("maxSurge")

        def _as_count(v, total):
            if v is None:
                return None
            if isinstance(v, str) and v.endswith("%"):
                return total * int(v[:-1]) / 100
            return float(v)

        mu, ms = _as_count(max_unavail, desired), _as_count(max_surge, desired)
        if mu is not None and desired and mu >= desired:
            signals.append(
                f"rolling update allows maxUnavailable={max_unavail} for {desired} replicas: all pods may go down at once"
            )
        if mu is not None and ms is not None and mu == 0 and ms == 0:
            signals.append("rolling update has maxUnavailable=0 and maxSurge=0: a rollout can never progress")
    for key in ("minReadySeconds", "progressDeadlineSeconds"):
        if spec.get(key) is not None:
            extra.setdefault("rollout", {})[key] = spec[key]

    config_refs: dict[str, set] = {"secrets": set(), "configmaps": set()}
    containers = []
    for c in (pod_spec.get("initContainers") or []) + pod_spec.get("containers", []):
        resources = c.get("resources", {}) or {}
        env_names = [e.get("name") for e in c.get("env", []) if e.get("name")]
        dupes = sorted({n for n in env_names if env_names.count(n) > 1})
        for n in dupes:
            signals.append(f"container {c.get('name')} defines env var {n} more than once (the last value wins)")
        env_from = []
        for ref in c.get("envFrom", []) or []:
            if ref.get("configMapRef"):
                env_from.append({"configMap": ref["configMapRef"].get("name"), "prefix": ref.get("prefix")})
                config_refs["configmaps"].add(ref["configMapRef"].get("name"))
            if ref.get("secretRef"):
                env_from.append({"secret": ref["secretRef"].get("name"), "prefix": ref.get("prefix")})
                config_refs["secrets"].add(ref["secretRef"].get("name"))
        for e in c.get("env", []) or []:
            vf = e.get("valueFrom") or {}
            if vf.get("secretKeyRef"):
                config_refs["secrets"].add(vf["secretKeyRef"].get("name"))
            if vf.get("configMapKeyRef"):
                config_refs["configmaps"].add(vf["configMapKeyRef"].get("name"))
        ports = [p.get("containerPort") for p in c.get("ports", []) if p.get("containerPort")]
        probes = {}
        for probe_key, label in (
            ("readinessProbe", "readiness"),
            ("livenessProbe", "liveness"),
            ("startupProbe", "startup"),
        ):
            probe = c.get(probe_key)
            if probe:
                probes[label] = _probe_summary(probe)
                port = _probe_port(probe)
                # Numeric probe ports need not be declared in `ports`; only a named port that
                # matches no declared port name is a definite misconfiguration.
                port_names = {p.get("name") for p in c.get("ports", []) if p.get("name")}
                if isinstance(port, str) and port not in port_names:
                    signals.append(
                        f"{label} probe of container {c.get('name')} references port name {port!r}, "
                        f"which is not among its declared port names {sorted(port_names)}"
                    )
        mounts = [m.get("mountPath") for m in c.get("volumeMounts") or [] if m.get("mountPath")]
        for path in sorted({m for m in mounts if mounts.count(m) > 1}):
            signals.append(f"container {c.get('name')} mounts more than one volume at {path}")
        containers.append(
            compact(
                {
                    "name": c.get("name"),
                    "init": c in (pod_spec.get("initContainers") or []),
                    "image": c.get("image"),
                    "limits": resources.get("limits") or {},
                    "requests": resources.get("requests") or {},
                    "ports": ports,
                    "env_names": env_names,
                    "env_from": env_from,
                    "probes": probes,
                    "mounts": mounts,
                    "command": c.get("command"),
                    "args": c.get("args"),
                }
            )
        )
    if pod_spec.get("restartPolicy") and pod_spec["restartPolicy"] != "Always":
        extra["restart_policy"] = pod_spec["restartPolicy"]
    for alias in pod_spec.get("hostAliases", []) or []:
        spec_flags.append(f"pod spec sets hostAliases: {alias.get('ip')} -> {', '.join(alias.get('hostnames', []))}")
    if pod_spec.get("nodeSelector"):
        spec_flags.append(f"nodeSelector set: {pod_spec['nodeSelector']}")
    if pod_spec.get("dnsPolicy") and pod_spec["dnsPolicy"] != "ClusterFirst":
        spec_flags.append(f"dnsPolicy is {pod_spec['dnsPolicy']}")
    if pod_spec.get("dnsConfig"):
        spec_flags.append(f"custom dnsConfig: {json.dumps(pod_spec['dnsConfig'])[:160]}")
    if pod_spec.get("hostNetwork"):
        spec_flags.append("hostNetwork enabled")
    for cond in status.get("conditions", []) or []:
        if cond.get("type") == "Progressing" and cond.get("status") == "False":
            spec_flags.append(f"Progressing=False ({cond.get('reason')}): {trim(cond.get('message'), 160)}")
        if cond.get("type") == "Available" and cond.get("status") == "False":
            spec_flags.append(f"Available=False ({cond.get('reason')})")

    volumes = []
    for v in pod_spec.get("volumes", []) or []:
        if "persistentVolumeClaim" in v:
            volumes.append({"name": v.get("name"), "pvc": v["persistentVolumeClaim"].get("claimName")})
        elif "configMap" in v:
            volumes.append({"name": v.get("name"), "configMap": v["configMap"].get("name")})
            config_refs["configmaps"].add(v["configMap"].get("name"))
        elif "secret" in v:
            volumes.append({"name": v.get("name"), "secret": v["secret"].get("secretName")})
            config_refs["secrets"].add(v["secret"].get("secretName"))
    for tmpl in spec.get("volumeClaimTemplates", []) or []:
        volumes.append({"pvc_template": tmpl.get("metadata", {}).get("name")})
    pvc_names = [v.get("pvc") for v in volumes if v.get("pvc")]
    for name in sorted({n for n in pvc_names if pvc_names.count(n) > 1}):
        signals.append(f"PVC {name} is mounted through more than one volume of this pod")

    return {
        "kind": kind,
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "created": meta.get("creationTimestamp"),
        "modified": last_update_time(meta),
        "service_account": pod_spec.get("serviceAccountName") or "default",
        "replicas": replicas,
        **extra,
        "selector": (spec.get("selector") or {}).get("matchLabels") or {},
        "template_labels": template.get("metadata", {}).get("labels") or {},
        "containers": containers,
        "volumes": volumes,
        "config_refs": {k: sorted(x for x in v if x) for k, v in config_refs.items()},
        "spec_flags": spec_flags,
        "pods": [],
        "services": [],
        "network_policies": [],
        "warning_events": [],
        "log_signals": [],
        "previous_container_logs": {},
        "resource_usage": [],
        "alerts": [],
        "signals": signals,
    }


def replica_signals(comp: dict) -> list[str]:
    if comp.get("kind") in ("CronJob", "Job"):
        return []
    r = comp["replicas"]
    desired, ready = r.get("desired", 0) or 0, r.get("ready", 0) or 0
    if desired == 0:
        return ["scaled to zero replicas (desired=0)"]
    if ready < desired:
        return [f"only {ready} of {desired} desired replicas are ready"]
    return []


# --------------------------------------------------------------------------- pods


def summarize_pod(pod: dict) -> dict:
    """Compact pod record plus the signals code can read off it."""
    meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
    statuses = (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or [])
    total = len(spec.get("containers", []))
    ready = sum(1 for s in status.get("containerStatuses") or [] if s.get("ready"))
    restarts = sum(s.get("restartCount", 0) for s in statuses)

    signals: list[str] = []
    container_states: list[str] = []
    phase = status.get("phase")
    if meta.get("deletionTimestamp"):
        signals.append("pod is terminating")
    if phase not in ("Running", "Succeeded"):
        reason = status.get("reason")
        signals.append(f"pod phase {phase}" + (f" ({reason})" if reason else ""))

    app_containers = {c.get("name") for c in spec.get("containers", [])}
    # Restarts whose last crash ended inside the warm-up window, on a pod whose containers are all
    # ready now and have been running for a while, are deploy-time churn rather than fault evidence.
    pod_start = parse_time(status.get("startTime"))
    now = datetime.now(UTC)
    settled = bool(pod_start) and phase == "Running" and bool(status.get("containerStatuses"))
    for s in status.get("containerStatuses") or []:
        last_fin = parse_time(((s.get("lastState") or {}).get("terminated") or {}).get("finishedAt"))
        run_since = parse_time(((s.get("state") or {}).get("running") or {}).get("startedAt"))
        if not s.get("ready"):
            settled = False
        elif s.get("restartCount", 0) > 0:
            if (
                last_fin is None
                or pod_start is None
                or last_fin > pod_start + timedelta(seconds=WARMUP_SECONDS)
                or run_since is None
                or (now - run_since).total_seconds() < 60
            ):
                settled = False
    startup_notes: list[str] = []
    for s in statuses:
        cname = s.get("name")
        state = s.get("state") or {}
        if "waiting" in state:
            reason = state["waiting"].get("reason") or "Waiting"
            msg = trim(state["waiting"].get("message"), 160)
            container_states.append(f"{cname}: waiting {reason}" + (f": {msg}" if msg else ""))
            if reason not in _BENIGN_WAITING_REASONS:
                signals.append(f"container {cname} waiting in {reason}")
        elif "terminated" in state:
            t = state["terminated"]
            container_states.append(f"{cname}: terminated {t.get('reason')} exit={t.get('exitCode')}")
            if t.get("exitCode") not in (0, None):
                signals.append(f"container {cname} terminated {t.get('reason')} exit={t.get('exitCode')}")
        elif phase == "Running" and cname in app_containers and not s.get("ready"):
            signals.append(f"container {cname} is running but not ready")
        last = (s.get("lastState") or {}).get("terminated")
        if last:
            container_states.append(f"{cname}: last terminated {last.get('reason')} exit={last.get('exitCode')}")
            if last.get("reason") == "OOMKilled":
                signals.append(f"container {cname} was OOMKilled")
        if s.get("restartCount", 0) > 0:
            note = f"container {cname} restarted {s['restartCount']} times"
            if settled:
                startup_notes.append(note + " during warm-up, then settled (ready since)")
            else:
                signals.append(note)

    finished = [s.get("name") for s in statuses if (s.get("state") or {}).get("terminated", {}).get("exitCode") == 0]
    still_running = [s.get("name") for s in statuses if "running" in (s.get("state") or {})]
    if phase == "Running" and finished and still_running and spec.get("restartPolicy") in ("Never", "OnFailure"):
        signals.append(
            f"container {', '.join(finished)} completed but container {', '.join(still_running)} keeps running, "
            "so this job pod never completes"
        )

    ready_since = next(
        (
            c.get("lastTransitionTime")
            for c in status.get("conditions") or []
            if c.get("type") == "Ready" and c.get("status") == "True"
        ),
        None,
    )
    conditions = []
    for cond in status.get("conditions") or []:
        if cond.get("status") != "True" and cond.get("type") in ("Ready", "PodScheduled", "ContainersReady"):
            text = f"{cond['type']}=False ({cond.get('reason')})"
            if cond.get("message"):
                text += f": {trim(cond['message'], 160)}"
            conditions.append(text)
            if cond.get("type") == "PodScheduled":
                signals.append(f"pod unschedulable: {trim(cond.get('message'), 160)}")

    return {
        "name": meta.get("name"),
        "labels": meta.get("labels") or {},
        "owner": [(o.get("kind"), o.get("name")) for o in meta.get("ownerReferences") or []],
        "phase": phase,
        "ready": f"{ready}/{total}",
        "restarts": restarts,
        "started": status.get("startTime"),
        "ready_since": ready_since,
        "node": spec.get("nodeName"),
        "container_states": container_states,
        "conditions": conditions,
        "startup_notes": startup_notes,
        "settled": settled,
        "signals": signals,
    }


def component_for_pod(
    pod_summary: dict, components: dict[str, dict], job_owner: dict[str, str] | None = None
) -> str | None:
    """Owner chain first (StatefulSet/DaemonSet/Job direct, ReplicaSet -> Deployment, Job -> CronJob), then selector."""
    job_owner = job_owner or {}
    for kind, name in pod_summary.get("owner", []):
        if kind in ("StatefulSet", "DaemonSet", "Job") and component_id(kind, name) in components:
            return component_id(kind, name)
        if kind == "Job" and f"{pod_summary.get('_namespace')}/{name}" in job_owner:
            return job_owner[f"{pod_summary.get('_namespace')}/{name}"]
        if kind == "ReplicaSet":
            dep = component_id("Deployment", _strip_hash_suffix(name, 1))
            if dep in components:
                return dep
    for cid, comp in components.items():
        if comp["namespace"] == pod_summary.get("_namespace") and selector_matches(
            comp["selector"], pod_summary["labels"]
        ):
            return cid
    return None


def component_for_object(
    kind: str, name: str, namespace: str, components: dict[str, dict], job_owner: dict[str, str] | None = None
) -> str | None:
    """Best-effort mapping for event targets that may already be gone."""
    if kind == "Job" and job_owner and f"{namespace}/{name}" in job_owner:
        return job_owner[f"{namespace}/{name}"]
    if kind in WORKLOAD_KINDS:
        cid = component_id(kind, name)
        if cid in components:
            return cid
        if kind == "Job":
            cron = component_id("CronJob", _strip_hash_suffix(name, 1))
            return cron if cron in components else None
        return None
    candidates: list[str] = []
    if kind == "ReplicaSet":
        candidates.append(component_id("Deployment", _strip_hash_suffix(name, 1)))
    elif kind == "Pod":
        candidates += [
            component_id("Deployment", _strip_hash_suffix(name, 2)),
            component_id("StatefulSet", _strip_hash_suffix(name, 1)),
            component_id("DaemonSet", _strip_hash_suffix(name, 1)),
            component_id("Job", _strip_hash_suffix(name, 1)),
            component_id("CronJob", _strip_hash_suffix(name, 2)),
        ]
    for cid in candidates:
        if cid in components and components[cid]["namespace"] == namespace:
            return cid
    return None


# --------------------------------------------------------------------------- events


def _event_time(ev: dict) -> str:
    return (
        ev.get("lastTimestamp")
        or (ev.get("series") or {}).get("lastObservedTime")
        or ev.get("eventTime")
        or ev.get("firstTimestamp")
        or ""
    )


def summarize_event(ev: dict) -> dict:
    obj = ev.get("involvedObject") or {}
    count = ev.get("count") or (ev.get("series") or {}).get("count") or 1
    return {
        "reason": ev.get("reason"),
        "count": count,
        "object": f"{(obj.get('kind') or '?').lower()}/{obj.get('name')}",
        "last_seen": _event_time(ev),
        "message": trim(ev.get("message")),
    }


def attach_warning_events(
    events: list[dict],
    components: dict[str, dict],
    unassigned: list[dict],
    job_owner: dict[str, str] | None = None,
    pod_index: dict[str, dict] | None = None,
) -> None:
    """Attach Warning events to components. Events from a settled pod's warm-up window are kept as history."""
    pod_index = pod_index or {}
    warnings = [e for e in events if e.get("type") == "Warning"]
    warnings.sort(key=_event_time, reverse=True)
    for ev in warnings:
        obj = ev.get("involvedObject") or {}
        cid = component_for_object(
            obj.get("kind", ""), obj.get("name", ""), obj.get("namespace", ""), components, job_owner
        )
        record = summarize_event(ev)
        if cid is None:
            if len(unassigned) < 10:
                unassigned.append(record)
            continue
        pod = pod_index.get(obj.get("name", "")) if obj.get("kind") == "Pod" else None
        if pod and pod.get("settled"):
            start, seen = parse_time(pod.get("started")), parse_time(record.get("last_seen"))
            if start and seen and seen <= start + timedelta(seconds=WARMUP_SECONDS):
                history = components[cid].setdefault("startup_events", [])
                if len(history) < 3:
                    history.append(record)
                continue
        bucket = components[cid]["warning_events"]
        if len(bucket) < MAX_EVENTS_PER_COMPONENT:
            bucket.append(record)
    for comp in components.values():
        if comp["warning_events"]:
            reasons: Counter[str] = Counter()
            for ev in comp["warning_events"]:
                reasons[ev["reason"]] += int(ev.get("count") or 1)
            summary = ", ".join(f"{r} x{n}" for r, n in reasons.most_common(4))
            comp["signals"].append(f"warning events: {summary}")


# --------------------------------------------------------------------------- services / netpol


def attach_services(
    services: list[dict],
    endpoints: list[dict],
    components: dict[str, dict],
    deploy_time: datetime | None = None,
    recent_changes: list[dict] | None = None,
) -> list[dict]:
    """Map services to the workloads they select and flag empty endpoints, dangling selectors, late changes."""
    ready_by_name: dict[tuple[str, str], tuple[int, int]] = {}
    for ep in endpoints:
        meta = ep.get("metadata", {})
        ready = sum(len(s.get("addresses") or []) for s in ep.get("subsets") or [])
        not_ready = sum(len(s.get("notReadyAddresses") or []) for s in ep.get("subsets") or [])
        ready_by_name[(meta.get("namespace"), meta.get("name"))] = (ready, not_ready)

    dangling: list[dict] = []
    for svc in services:
        meta, spec = svc.get("metadata", {}), svc.get("spec", {})
        if spec.get("type") == "ExternalName" or not spec.get("selector"):
            continue
        ns, name = meta.get("namespace"), meta.get("name")
        ready, not_ready = ready_by_name.get((ns, name), (0, 0))
        record = {
            "name": name,
            "type": spec.get("type"),
            "ports": [
                f"{p.get('port')}->{p.get('targetPort')}/{p.get('protocol', 'TCP')}" for p in spec.get("ports", [])
            ],
            "selector": spec["selector"],
            "ready_endpoints": ready,
            "not_ready_endpoints": not_ready,
        }
        policies = []
        modified = last_update_time(meta)
        late = change_after_deploy(modified, deploy_time)
        if late is not None:
            record["modified"] = modified
            policies.append(f"service {name} was modified {late}s after the application was deployed")
            if recent_changes is not None:
                recent_changes.append(
                    {"object": f"service/{ns}/{name}", "modified": modified, "seconds_after_deploy": late}
                )
        if spec.get("internalTrafficPolicy") and spec["internalTrafficPolicy"] != "Cluster":
            record["internal_traffic_policy"] = spec["internalTrafficPolicy"]
            policies.append(
                f"service {name} has internalTrafficPolicy={spec['internalTrafficPolicy']}: only endpoints on the "
                "client's own node receive traffic"
            )
        if spec.get("externalTrafficPolicy") and spec["externalTrafficPolicy"] != "Cluster":
            record["external_traffic_policy"] = spec["externalTrafficPolicy"]
        if spec.get("sessionAffinity") and spec["sessionAffinity"] != "None":
            record["session_affinity"] = spec["sessionAffinity"]
        if spec.get("publishNotReadyAddresses"):
            record["publish_not_ready_addresses"] = True
        owners = [
            cid
            for cid, comp in components.items()
            if comp["namespace"] == ns and selector_matches(spec["selector"], comp["template_labels"])
        ]
        for cid in owners:
            components[cid]["services"].append(record)
            if ready == 0:
                components[cid]["signals"].append(f"service {name} selects this component but has 0 ready endpoints")
            # A DaemonSet is node-local by design, so a Local traffic policy is idiomatic there.
            if components[cid]["kind"] != "DaemonSet":
                components[cid]["signals"].extend(policies)
        if not owners:
            record["matches_no_workload"] = True
            dangling.append(record)
            # A service named after a workload whose selector no longer matches it is a classic mis-wiring.
            for comp in components.values():
                if comp["namespace"] == ns and comp["name"] == name:
                    comp["services"].append(record)
                    comp["signals"].append(
                        f"service {name} selector {spec['selector']} does not match this workload's pod labels "
                        f"{comp['template_labels']}"
                    )
                    comp["signals"].extend(policies)
    return dangling


def summarize_network_policy(policy: dict) -> dict:
    meta, spec = policy.get("metadata", {}), policy.get("spec", {})
    types = spec.get("policyTypes") or (["Ingress"] if "ingress" in spec or "egress" not in spec else []) or []
    ingress, egress = spec.get("ingress"), spec.get("egress")
    effects = []
    if "Ingress" in types:
        effects.append("denies all ingress" if not ingress else f"restricts ingress to {len(ingress)} rule(s)")
    if "Egress" in types:
        effects.append("denies all egress" if not egress else f"restricts egress to {len(egress)} rule(s)")
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "pod_selector": (spec.get("podSelector") or {}).get("matchLabels") or {},
        "selects_all_pods": not (spec.get("podSelector") or {}).get("matchLabels")
        and not (spec.get("podSelector") or {}).get("matchExpressions"),
        "policy_types": types,
        "effect": "; ".join(effects) or "no effect",
        "ingress_rules": ingress if ingress else [],
        "egress_rules": egress if egress else [],
    }


def attach_network_policies(
    policies: list[dict], components: dict[str, dict], deploy_time: datetime | None = None
) -> list[dict]:
    summaries = []
    for raw in policies:
        pol = summarize_network_policy(raw)
        pol["created"] = raw.get("metadata", {}).get("creationTimestamp")
        late = change_after_deploy(pol["created"], deploy_time)
        if late is not None:
            pol["seconds_after_deploy"] = late
        summaries.append(pol)
    for pol in summaries:
        for comp in components.values():
            if comp["namespace"] != pol["namespace"]:
                continue
            if pol["selects_all_pods"] or selector_matches(pol["pod_selector"], comp["template_labels"]):
                comp["network_policies"].append({k: v for k, v in pol.items() if k not in ("namespace",)})
                late = (
                    f", created {pol['seconds_after_deploy']}s after the application was deployed"
                    if pol.get("seconds_after_deploy")
                    else ""
                )
                comp["signals"].append(f"selected by NetworkPolicy {pol['name']} ({pol['effect']}{late})")
    return summaries


# --------------------------------------------------------------------------- hpa / pvc / nodes


def attach_hpas(hpas: list[dict], components: dict[str, dict]) -> None:
    for hpa in hpas:
        spec, status, meta = hpa.get("spec", {}), hpa.get("status", {}), hpa.get("metadata", {})
        ref = spec.get("scaleTargetRef") or {}
        cid = component_id(ref.get("kind", ""), ref.get("name", ""))
        if cid not in components or components[cid]["namespace"] != meta.get("namespace"):
            continue
        bad = [
            f"{c.get('type')}=False ({c.get('reason')}): {trim(c.get('message'), 120)}"
            for c in status.get("conditions") or []
            if c.get("status") == "False" and c.get("type") in ("AbleToScale", "ScalingActive")
        ]
        components[cid]["hpa"] = {
            "name": meta.get("name"),
            "min": spec.get("minReplicas"),
            "max": spec.get("maxReplicas"),
            "current": status.get("currentReplicas"),
            "desired": status.get("desiredReplicas"),
            "problems": bad,
        }
        if bad:
            components[cid]["signals"].append(f"HPA {meta.get('name')} unhealthy: {bad[0]}")
        if spec.get("maxReplicas") and status.get("desiredReplicas") == spec.get("maxReplicas"):
            components[cid]["signals"].append(f"HPA {meta.get('name')} is pinned at maxReplicas={spec['maxReplicas']}")


def attach_pvcs(pvcs: list[dict], components: dict[str, dict]) -> None:
    phase_by_name = {
        (p.get("metadata", {}).get("namespace"), p.get("metadata", {}).get("name")): p.get("status", {}).get("phase")
        for p in pvcs
    }
    for comp in components.values():
        for vol in comp["volumes"]:
            name = vol.get("pvc")
            if not name:
                continue
            phase = phase_by_name.get((comp["namespace"], name))
            vol["phase"] = phase
            if phase and phase != "Bound":
                comp["signals"].append(f"PVC {name} is {phase}")


def summarize_nodes(nodes: list[dict]) -> list[dict]:
    out = []
    for node in nodes:
        meta, spec, status = node.get("metadata", {}), node.get("spec", {}), node.get("status", {})
        problems = []
        for cond in status.get("conditions") or []:
            ctype, cstatus = cond.get("type"), cond.get("status")
            if (ctype == "Ready" and cstatus != "True") or (ctype in _PRESSURE_CONDITIONS and cstatus == "True"):
                problems.append(f"{ctype}={cstatus} ({cond.get('reason')})")
        if spec.get("unschedulable"):
            problems.append("cordoned (unschedulable)")
        taints = [
            f"{t.get('key')}={t.get('value')}:{t.get('effect')}"
            for t in spec.get("taints") or []
            if "node-role.kubernetes.io/control-plane" not in (t.get("key") or "")
        ]
        out.append({"name": meta.get("name"), "problems": problems, "taints": taints})
    return out


# --------------------------------------------------------------------------- logs


def log_line_time(line: str) -> datetime | None:
    """Timestamp kubectl prepends with --timestamps (after the [pod/…/container] prefix)."""
    text = _PREFIX_RE.sub("", line, count=1)
    m = _RFC3339_RE.match(text.lstrip())
    return parse_time(m.group(1)) if m else None


def normalize_log_line(line: str) -> tuple[str, str]:
    """Return (dedupe_key, display_text) for a raw `kubectl logs --prefix` line."""
    text = line.rstrip()
    container = None
    m = _PREFIX_RE.match(text)
    if m:
        container = m.group(1)
        text = text[m.end() :]
    text = _TIMESTAMP_RE.sub("", text).strip()
    display = trim(f"{container}: {text}" if container else text)
    key = _UUID_RE.sub("<id>", text)
    key = _HEX_RE.sub("<hex>", key)
    key = _ADDR_RE.sub("<addr>", key)
    key = _NUM_RE.sub("<n>", key)
    key = _WS_RE.sub(" ", key).lower()
    return (f"{container}|{key}" if container else key), display


def is_error_line(line: str) -> bool:
    if not line.strip() or not _ERROR_LINE_RE.search(line) or _BENIGN_LINE_RE.search(line):
        return False
    return not _INFO_LEVEL_RE.search(line)


def extract_log_signals(
    raw_logs: Iterable[str], limit: int = MAX_LOG_SIGNALS, *, now: datetime | None = None
) -> list[dict]:
    """Error-looking lines, deduplicated with counts, most frequent first, with recency computed in code."""
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    latest: dict[str, datetime] = {}
    for line in raw_logs:
        if not is_error_line(line):
            continue
        key, display = normalize_log_line(line)
        counts[key] += 1
        examples.setdefault(key, display)
        ts = log_line_time(line)
        if ts and (key not in latest or ts > latest[key]):
            latest[key] = ts
    now = now or datetime.now(UTC)
    out = []
    for k, n in counts.most_common(limit):
        item = {"count": n, "line": examples[k]}
        if k in latest:
            item["last_seen_seconds_ago"] = max(0, int((now - latest[k]).total_seconds()))
        out.append(item)
    return out


def warmup_cutoff(pod: dict) -> datetime | None:
    """Lines before this moment are startup noise: the pod's first Ready transition, capped at WARMUP_SECONDS.

    A pod that is not Ready now is actively failing, so none of its lines are noise.
    """
    ready = pod.get("ready", "0/0").split("/")
    if len(ready) != 2 or ready[0] != ready[1] or ready[1] == "0":
        return None
    start, ready_since = parse_time(pod.get("started")), parse_time(pod.get("ready_since"))
    if ready_since is not None:
        cutoff = ready_since
        if start is not None:
            cutoff = min(cutoff, start + timedelta(seconds=WARMUP_SECONDS))
        return cutoff
    return start + timedelta(seconds=WARMUP_SECONDS) if start else None


def split_warmup_lines(lines: list[str], cutoff: datetime | None) -> tuple[list[str], int]:
    """Drop lines logged before `cutoff`. Returns (kept, dropped_error_lines)."""
    if cutoff is None:
        return lines, 0
    kept, dropped = [], 0
    for line in lines:
        ts = log_line_time(line)
        if ts is not None and ts < cutoff:
            if is_error_line(line):
                dropped += 1
            continue
        kept.append(line)
    return kept, dropped


def _fetch_pod_logs(pod: str, namespace: str, tail: int) -> list[str]:
    out = run_kubectl(
        ["logs", pod, "-n", namespace, "--all-containers=true", "--prefix=true", "--timestamps=true", f"--tail={tail}"],
        timeout=LOG_TIMEOUT,
    )
    return out.splitlines()


def _fetch_previous_logs(pod: str, container: str, namespace: str) -> list[str]:
    out = run_kubectl(
        ["logs", pod, "-n", namespace, "-c", container, "--previous", f"--tail={MAX_PREVIOUS_LOG_LINES * 3}"],
        timeout=LOG_TIMEOUT,
    )
    lines = [trim(_TIMESTAMP_RE.sub("", ln)) for ln in out.splitlines() if ln.strip()]
    return lines[-MAX_PREVIOUS_LOG_LINES:]


def attach_logs(
    components: dict[str, dict], pods_by_component: dict[str, list[dict]], errors: list[str], tail: int
) -> None:
    """Fetch recent logs for up to two pods per component; keep error-looking lines logged after warm-up."""
    jobs: list[tuple[str, Callable[[], object], str, datetime | None]] = []
    for cid, pods in pods_by_component.items():
        ns = components[cid]["namespace"]
        loggable = [p for p in pods if p["phase"] == "Running"]
        loggable.sort(key=lambda p: (-p["restarts"], p["name"]))
        for pod in loggable[:MAX_LOG_PODS_PER_COMPONENT]:
            jobs.append((cid, lambda p=pod["name"], n=ns: _fetch_pod_logs(p, n, tail), "current", warmup_cutoff(pod)))
            for state in pod["container_states"]:
                if "last terminated" in state:
                    cname = state.split(":", 1)[0]
                    jobs.append(
                        (
                            cid,
                            lambda p=pod["name"], c=cname, n=ns: (c, _fetch_previous_logs(p, c, n)),
                            "previous",
                            None,
                        )
                    )
    raw_by_component: dict[str, list[str]] = {cid: [] for cid in components}
    dropped_by_component: Counter[str] = Counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [(cid, kind, cutoff, pool.submit(fn)) for cid, fn, kind, cutoff in jobs]
        for cid, kind, cutoff, fut in futures:
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"logs for {cid}: {exc}")
                continue
            if kind == "current":
                kept, dropped = split_warmup_lines(result, cutoff)  # type: ignore[arg-type]
                raw_by_component[cid].extend(kept)
                dropped_by_component[cid] += dropped
            else:
                cname, lines = result  # type: ignore[misc]
                if lines:
                    components[cid]["previous_container_logs"][cname] = lines
    for cid, lines in raw_by_component.items():
        signals = extract_log_signals(lines)
        components[cid]["log_signals"] = signals
        if signals:
            # Kept apart from `signals`: error-like log lines are weak evidence, since most components of a
            # microservice application log errors whenever any dependency misbehaves.
            components[cid]["log_error_lines"] = sum(s["count"] for s in signals)
        if dropped_by_component[cid]:
            components[cid]["warmup_log_errors_ignored"] = dropped_by_component[cid]


# --------------------------------------------------------------------------- resource usage


def attach_resource_usage(top_output: str, components: dict[str, dict], pod_to_component: dict[str, str]) -> None:
    """Parse `kubectl top pods --containers` and compare usage with declared limits in code."""
    for line in top_output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        pod, container, cpu, mem = parts[0], parts[1], parts[2], parts[3]
        cid = pod_to_component.get(pod)
        if cid is None:
            continue
        comp = components[cid]
        limits = next((c.get("limits") or {} for c in comp["containers"] if c.get("name") == container), {})
        record = {
            "pod": pod,
            "container": container,
            "cpu": cpu,
            "memory": mem,
            "cpu_limit": limits.get("cpu"),
            "memory_limit": limits.get("memory"),
        }
        comp["resource_usage"].append(record)
        cpu_used, cpu_lim = parse_cpu_millis(cpu), parse_cpu_millis(limits.get("cpu"))
        if cpu_used is not None and cpu_lim:
            if cpu_used >= 0.9 * cpu_lim:
                comp["signals"].append(
                    f"container {container} cpu usage {cpu} is at its limit {limits.get('cpu')} (throttling likely)"
                )
        mem_used, mem_lim = parse_memory_bytes(mem), parse_memory_bytes(limits.get("memory"))
        if mem_used is not None and mem_lim and mem_used >= 0.9 * mem_lim:
            comp["signals"].append(f"container {container} memory usage {mem} is near its limit {limits.get('memory')}")


# --------------------------------------------------------------------------- alerts


_ALERT_LABEL_KEYS = (
    "namespace",
    "service_name",
    "service",
    "deployment",
    "k8s_deployment_name",
    "app",
    "pod",
    "container",
    "job",
    "severity",
)


def parse_alerts(raw: str) -> list[dict]:
    """Parse the Prometheus MCP `get_alerts` text (a Python literal list) into compact records."""
    if not raw or raw.strip().startswith("No firing alerts") or raw.strip().startswith("[prom_mcp] Error"):
        return []
    try:
        alerts = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        try:
            alerts = json.loads(raw)
        except ValueError:
            return []
    out = []
    for a in alerts if isinstance(alerts, list) else []:
        labels = a.get("labels") or {}
        ann = a.get("annotations") or {}
        out.append(
            {
                "alertname": labels.get("alertname"),
                "labels": {k: labels[k] for k in _ALERT_LABEL_KEYS if k in labels},
                "summary": trim(ann.get("summary") or ann.get("description") or ann.get("message"), 200),
                "active_since": a.get("activeAt"),
            }
        )
    return out


def attach_alerts(alerts: list[dict], components: dict[str, dict]) -> None:
    for alert in alerts:
        labels = alert["labels"]
        targets = {labels.get(k) for k in ("service_name", "service", "deployment", "k8s_deployment_name", "app")} - {
            None
        }
        for comp in components.values():
            if labels.get("namespace") and labels["namespace"] != comp["namespace"]:
                continue
            pod = labels.get("pod") or ""
            if comp["name"] in targets or (pod and pod.startswith(comp["name"] + "-")):
                comp["alerts"].append(alert["alertname"])
                comp["signals"].append(f"firing alert {alert['alertname']}")


def fetch_alerts_via_mcp(timeout: float = 20.0) -> str:
    """Call the Prometheus MCP `get_alerts` tool. Returns the raw text ("" on any failure)."""
    import asyncio
    import os

    from fastmcp import Client
    from fastmcp.client import SSETransport

    url = f"http://{os.getenv('API_HOSTNAME', 'localhost')}:{os.getenv('MCP_SERVER_PORT', '9954')}/prometheus/sse"

    async def _call() -> str:
        async with Client(SSETransport(url=url, sse_read_timeout=timeout)) as client:
            result = await client.call_tool("get_alerts", arguments={}, timeout=timeout)
            return "\n".join(getattr(part, "text", "") for part in result)

    return asyncio.run(asyncio.wait_for(_call(), timeout=timeout + 5))


# --------------------------------------------------------------------------- namespace constraints


def summarize_namespace_constraints(limit_ranges: list[dict], quotas: list[dict]) -> list[dict]:
    """LimitRanges and ResourceQuotas, with quota pressure computed in code."""
    out = []
    for lr in limit_ranges:
        meta = lr.get("metadata", {})
        out.append(
            {
                "kind": "LimitRange",
                "namespace": meta.get("namespace"),
                "name": meta.get("name"),
                "created": meta.get("creationTimestamp"),
                "limits": (lr.get("spec") or {}).get("limits"),
                "signals": [],
            }
        )
    for q in quotas:
        meta, status = q.get("metadata", {}), q.get("status", {})
        hard, used = status.get("hard") or {}, status.get("used") or {}
        signals = []
        for res, hard_v in hard.items():
            used_v = used.get(res)
            parse = (
                parse_cpu_millis
                if "cpu" in res
                else parse_memory_bytes
                if "memory" in res or "storage" in res
                else None
            )
            if parse is None:
                try:
                    h, u = float(hard_v), float(used_v)
                except (TypeError, ValueError):
                    continue
            else:
                h, u = parse(hard_v), parse(used_v)
            if h and u is not None and u >= 0.9 * h:
                signals.append(f"quota {res}: used {used_v} of hard limit {hard_v}")
        out.append(
            {
                "kind": "ResourceQuota",
                "namespace": meta.get("namespace"),
                "name": meta.get("name"),
                "created": meta.get("creationTimestamp"),
                "hard": hard,
                "used": used,
                "signals": signals,
            }
        )
    return out


def attach_namespace_constraints(constraints: list[dict], components: dict[str, dict]) -> None:
    """Quota pressure and LimitRange defaults are namespace-wide; note them on the affected components."""
    for c in constraints:
        ns_components = [comp for comp in components.values() if comp["namespace"] == c["namespace"]]
        if c["kind"] == "ResourceQuota" and c["signals"]:
            for comp in ns_components:
                if not comp["healthy_hint"]:
                    comp["spec_flags"].append(
                        f"namespace ResourceQuota {c['name']} is near its limit: {c['signals'][0]}"
                    )
        if c["kind"] == "LimitRange":
            for comp in ns_components:
                if any(not (ct.get("limits") or {}).get("memory") for ct in comp["containers"] if not ct.get("init")):
                    comp["spec_flags"].append(
                        f"LimitRange {c['name']} applies to this namespace and this workload sets no memory limit "
                        f"(defaults/max: {json.dumps(c['limits'])[:200]})"
                    )


# --------------------------------------------------------------------------- admission webhooks


def summarize_webhooks(configs: list[dict], errors: list[str]) -> list[dict]:
    """Mutating/validating webhooks and whether their backend Service has endpoints (checked in code)."""
    out = []
    endpoint_cache: dict[tuple[str, str], int | None] = {}
    for cfg in configs:
        kind = cfg.get("kind", "")
        for wh in cfg.get("webhooks") or []:
            svc = ((wh.get("clientConfig") or {}).get("service")) or {}
            key = (svc.get("namespace"), svc.get("name"))
            ready = None
            if svc.get("name"):
                if key not in endpoint_cache:
                    try:
                        ep = json.loads(
                            run_kubectl(["get", "endpoints", svc["name"], "-n", svc["namespace"], "-o", "json"])
                        )
                        endpoint_cache[key] = sum(len(sub.get("addresses") or []) for sub in ep.get("subsets") or [])
                    except Exception as exc:  # noqa: BLE001 - missing service is itself a finding
                        endpoint_cache[key] = 0
                        errors.append(f"webhook backend {key}: {exc}")
                ready = endpoint_cache[key]
            rules = [
                f"{'/'.join(r.get('operations') or [])} {','.join(r.get('resources') or [])}"
                for r in wh.get("rules") or []
            ]
            record = {
                "kind": kind,
                "configuration": cfg.get("metadata", {}).get("name"),
                "created": cfg.get("metadata", {}).get("creationTimestamp"),
                "webhook": wh.get("name"),
                "failure_policy": wh.get("failurePolicy"),
                "timeout_seconds": wh.get("timeoutSeconds"),
                "rules": rules,
                "namespace_selector": (wh.get("namespaceSelector") or {}) or None,
                "object_selector": (wh.get("objectSelector") or {}) or None,
                "backend": {"namespace": svc.get("namespace"), "service": svc.get("name"), "port": svc.get("port")}
                if svc
                else {"url": (wh.get("clientConfig") or {}).get("url")},
                "backend_ready_endpoints": ready,
                "signals": [],
            }
            if svc.get("name") and ready == 0:
                record["signals"].append(
                    f"{kind} {wh.get('name')} (failurePolicy={wh.get('failurePolicy')}) backend service "
                    f"{svc.get('namespace')}/{svc.get('name')} has 0 ready endpoints; matching admission requests "
                    f"({'; '.join(rules)}) {'fail' if wh.get('failurePolicy') == 'Fail' else 'are skipped'}"
                )
            elif kind == "MutatingWebhookConfiguration":
                record["signals"].append(
                    f"mutating webhook {wh.get('name')} rewrites objects matching {'; '.join(rules) or 'its rules'}"
                )
            out.append(record)
    return out[:12]


def attach_webhooks(webhooks: list[dict], components: dict[str, dict]) -> None:
    for wh in webhooks:
        backend = wh.get("backend") or {}
        for comp in components.values():
            if comp["namespace"] != backend.get("namespace"):
                continue
            if any(svc.get("name") == backend.get("service") for svc in comp["services"]):
                comp["spec_flags"].append(
                    f"serves admission webhook {wh['webhook']} ({wh['kind']}, failurePolicy={wh['failure_policy']})"
                )
                if wh.get("backend_ready_endpoints") == 0:
                    comp["signals"].append(f"admission webhook backend for {wh['webhook']} has no ready endpoints")


# --------------------------------------------------------------------------- cluster DNS


def summarize_cluster_dns(errors: list[str], deploy_time: datetime | None) -> dict:
    """Cluster DNS (CoreDNS) configuration: a classic single point of failure outside the app namespaces."""
    out: dict = {}
    try:
        cm = json.loads(
            run_kubectl(["get", "configmap", "coredns", "-n", "kube-system", "-o", "json", "--show-managed-fields"])
        )
    except Exception as exc:  # noqa: BLE001 - not every cluster runs CoreDNS
        errors.append(f"coredns configmap: {exc}")
        return out
    meta = cm.get("metadata", {})
    corefile = (cm.get("data") or {}).get("Corefile", "")
    out = {
        "configmap": "kube-system/coredns",
        "modified": last_update_time(meta),
        "corefile_lines": len(corefile.splitlines()),
        "plugins": sorted(
            {
                ln.strip().split()[0]
                for ln in corefile.splitlines()
                if ln.strip() and not ln.strip().startswith(("}", ".", "#")) and "{" not in ln
            }
        ),
        "template_or_rewrite_rules": [
            trim(ln, 160) for ln in corefile.splitlines() if ln.strip().startswith(("template", "rewrite", "hosts"))
        ],
    }
    late = change_after_deploy(out["modified"], deploy_time)
    if late is not None:
        out["seconds_after_deploy"] = late
        out["signals"] = [
            f"CoreDNS ConfigMap was modified {late}s after the application was deployed; cluster name resolution may be altered"
        ]
    try:
        pods = json.loads(
            run_kubectl(["get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns", "-o", "json"])
        ).get("items", [])
        not_ready = [
            p["metadata"]["name"]
            for p in pods
            if not all(c.get("ready") for c in p.get("status", {}).get("containerStatuses") or [])
        ]
        out["pods"] = len(pods)
        if not_ready:
            out.setdefault("signals", []).append(f"CoreDNS pods not ready: {', '.join(not_ready)}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"coredns pods: {exc}")
    return out


# --------------------------------------------------------------------------- config objects


def summarize_config_objects(secrets: list[dict], configmaps: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Metadata and key names only. Values are never read into the state."""
    out = {}
    for kind, items in (("Secret", secrets), ("ConfigMap", configmaps)):
        for obj in items:
            meta = obj.get("metadata", {})
            out[(kind, meta.get("namespace"), meta.get("name"))] = {
                "type": obj.get("type"),
                "keys": sorted((obj.get("data") or {}).keys()) + sorted((obj.get("stringData") or {}).keys()),
                "created": meta.get("creationTimestamp"),
                "last_update": last_update_time(meta),
            }
    return out


def attach_config_objects(
    objects: dict[tuple[str, str, str], dict], components: dict[str, dict], deploy_time: datetime | None = None
) -> None:
    """Post-deploy changes, staleness (changed after the pod started) and env shadowing, computed in code."""
    for comp in components.values():
        ns = comp["namespace"]
        pod_starts = [parse_time(p.get("started")) for p in comp.get("pods", [])]
        pod_starts = [t for t in pod_starts if t]
        earliest = min(pod_starts) if pod_starts else None
        refs = comp.get("config_refs") or {}
        details = {}
        for kind, key in (("Secret", "secrets"), ("ConfigMap", "configmaps")):
            for name in refs.get(key, []):
                obj = objects.get((kind, ns, name))
                if obj is None:
                    comp["signals"].append(f"referenced {kind} {name} does not exist in namespace {ns}")
                    continue
                details[f"{kind.lower()}/{name}"] = {"keys": obj["keys"][:20], "last_update": obj["last_update"]}
                updated = parse_time(obj["last_update"])
                late = change_after_deploy(obj["last_update"], deploy_time)
                if late is not None:
                    comp["signals"].append(
                        f"referenced {kind} {name} was modified {late}s after the application was deployed "
                        f"(keys: {', '.join(obj['keys'][:8]) or 'none'})"
                    )
                if earliest and updated and updated > earliest:
                    comp["signals"].append(
                        f"{kind} {name} was modified at {obj['last_update']}, after this workload's pods started "
                        f"at {earliest.isoformat()}; values read at startup are stale"
                    )
        if details:
            comp["config_objects"] = details
        for ct in comp.get("containers", []):
            env_names = set(ct.get("env_names") or [])
            for ref in ct.get("env_from") or []:
                kind = "ConfigMap" if ref.get("configMap") else "Secret"
                obj = objects.get((kind, ns, ref.get("configMap") or ref.get("secret")))
                if not obj:
                    continue
                prefix = ref.get("prefix") or ""
                shadowed = sorted(env_names & {prefix + k for k in obj["keys"]})
                for var in shadowed:
                    comp["signals"].append(
                        f"container {ct.get('name')}: env var {var} is set explicitly and also provided by "
                        f"{kind} {ref.get('configMap') or ref.get('secret')} via envFrom; the explicit value shadows it"
                    )


# --------------------------------------------------------------------------- orchestration


_RBAC_DENIED_RE = re.compile(
    r"(?i)(is forbidden|\bforbidden\b|\b403\b|cannot (?:get|list|watch|create|update|patch|delete) resource|"
    r"\bRBAC\b|not allowed to|PermissionDenied)"
)
_DENIED_OPERATION_RE = re.compile(
    r'(?i)cannot (?P<verb>get|list|watch|create|update|patch|delete|deletecollection) (?:resource )?"?'
    r"(?P<resource>[a-z][a-z0-9./-]*)"
)
MAX_RBAC_RULES = 8


def _rule_lines(rules: list[dict]) -> list[str]:
    out = []
    for rule in rules or []:
        resources = ",".join(rule.get("resources") or rule.get("nonResourceURLs") or ["*"])
        verbs = ",".join(rule.get("verbs") or [])
        groups = ",".join(g or "core" for g in rule.get("apiGroups") or [])
        out.append(f"{resources} [{groups}]: {verbs}" if groups else f"{resources}: {verbs}")
    return out[:MAX_RBAC_RULES]


def _grants(rules: list[dict], verb: str, resource: str) -> bool:
    resource = resource.split("/")[0].split(".")[0].lower()
    for rule in rules or []:
        res = [r.lower() for r in rule.get("resources") or []]
        verbs = [v.lower() for v in rule.get("verbs") or []]
        if ("*" in res or resource in res) and ("*" in verbs or verb.lower() in verbs):
            return True
    return False


def summarize_rbac(
    namespaces: list[str], components: dict[str, dict], errors: list[str], deploy_time: datetime | None
) -> list[dict]:
    """Roles and ClusterRoles reachable from the ServiceAccounts the application's workloads run as.

    Only roles bound to a workload's ServiceAccount are fetched. A role gets a signal when it was written
    after the application was deployed, or when a workload bound to it logs authorization denials; when the
    denied verb and resource can be read from those logs, the role's rules are checked for them in code.
    """
    sa_to_components: dict[tuple[str, str], list[str]] = {}
    for cid, comp in components.items():
        key = (comp["namespace"], comp.get("service_account") or "default")
        sa_to_components.setdefault(key, []).append(cid)
    bindings: list[tuple[str, dict]] = [
        ("ClusterRoleBinding", b) for b in kubectl_items("clusterrolebindings", None, errors, cluster_scoped=True)
    ]
    for ns in namespaces:
        bindings += [("RoleBinding", b) for b in kubectl_items("rolebindings", ns, errors)]
    entries: dict[tuple[str, str | None, str], dict] = {}
    for bkind, b in bindings:
        bmeta = b.get("metadata", {})
        bound: list[str] = []
        for subj in b.get("subjects") or []:
            if subj.get("kind") != "ServiceAccount":
                continue
            key = (subj.get("namespace") or bmeta.get("namespace"), subj.get("name"))
            bound += sa_to_components.get(key, [])
        if not bound:
            continue
        ref = b.get("roleRef") or {}
        rkind, rname = ref.get("kind"), ref.get("name")
        if rkind not in ("Role", "ClusterRole") or not rname:
            continue
        rns = bmeta.get("namespace") if rkind == "Role" else None
        ekey = (rkind, rns, rname)
        if ekey not in entries:
            args = ["get", rkind.lower(), rname, "-o", "json", "--show-managed-fields"] + (["-n", rns] if rns else [])
            try:
                role = json.loads(run_kubectl(args))
            except Exception as exc:  # noqa: BLE001 - a missing role is itself a finding
                role = {}
                errors.append(f"rbac {rkind}/{rname}: {str(exc)[:160]}")
            rmeta = role.get("metadata", {})
            late = change_after_deploy(last_update_time(rmeta), deploy_time)
            entries[ekey] = {
                "kind": rkind,
                "name": rname,
                "namespace": rns,
                "bindings": [],
                "service_accounts": [],
                "components": [],
                "rules": _rule_lines(role.get("rules") or []) if role else ["(role not found)"],
                "_rules": role.get("rules") or [],
                "created": rmeta.get("creationTimestamp"),
                "modified": last_update_time(rmeta),
                "seconds_after_deploy": late,
                "signals": [],
            }
            if late is not None:
                entries[ekey]["signals"].append(f"written {late}s after the application was deployed")
        entry = entries[ekey]
        entry["bindings"].append(f"{bkind}/{bmeta.get('name')}")
        for subj in b.get("subjects") or []:
            if subj.get("kind") == "ServiceAccount":
                entry["service_accounts"].append(
                    f"{subj.get('namespace') or bmeta.get('namespace')}/{subj.get('name')}"
                )
        entry["components"] = sorted(set(entry["components"]) | set(bound))
    for entry in entries.values():
        for cid in entry["components"]:
            comp = components.get(cid) or {}
            denied = [s for s in comp.get("log_signals") or [] if _RBAC_DENIED_RE.search(s.get("line") or "")]
            if not denied:
                continue
            count = sum(int(s.get("count") or 1) for s in denied)
            entry["signals"].append(f"bound workload {cid} logs authorization denials ({count} lines)")
            comp.setdefault("signals", []).append(
                f"service account {comp.get('namespace')}/{comp.get('service_account') or 'default'} is bound to "
                f"{entry['kind']}/{entry['name']} granting: {'; '.join(entry['rules'][:4]) or 'nothing'}"
            )
            for s in denied:
                m = _DENIED_OPERATION_RE.search(s.get("line") or "")
                if not m:
                    continue
                verb, resource = m.group("verb").lower(), m.group("resource").lower()
                if _grants(entry["_rules"], verb, resource):
                    entry["signals"].append(
                        f"the denied operation '{verb} {resource}' is granted by this role, so another rule set denies it"
                    )
                else:
                    entry["signals"].append(
                        f"the denied operation '{verb} {resource}' is not granted by this role (rules: "
                        f"{'; '.join(entry['rules'][:4]) or 'none'})"
                    )
                break
    out = []
    for entry in entries.values():
        entry.pop("_rules", None)
        entry["signals"] = list(dict.fromkeys(entry["signals"]))
        entry["bindings"] = sorted(set(entry["bindings"]))
        entry["service_accounts"] = sorted(set(entry["service_accounts"]))
        out.append(entry)
    return out


def collect_snapshot(
    app_info: dict,
    *,
    log_tail: int = DEFAULT_LOG_TAIL,
    alerts_fetcher: Callable[[], str] | None = fetch_alerts_via_mcp,
    include_logs: bool = True,
) -> ClusterSnapshot:
    """Read the cluster once and return the untrimmed snapshot. Never raises for a single failed read."""
    errors: list[str] = []
    namespaces = list(app_info.get("namespaces") or [app_info.get("namespace")])
    namespaces = [ns for ns in namespaces if ns]
    components: dict[str, dict] = {}
    pods_by_component: dict[str, list[dict]] = {}
    pod_to_component: dict[str, str] = {}
    pod_index: dict[str, dict] = {}
    unassigned_events: list[dict] = []
    unassigned_pods: list[dict] = []
    dangling_services: list[dict] = []
    all_policies: list[dict] = []

    job_owner: dict[str, str] = {}  # "ns/job-name" -> CronJob component id
    for ns in namespaces:
        for resource in ("deployments", "statefulsets", "daemonsets", "cronjobs"):
            for obj in kubectl_items(resource, ns, errors):
                comp = summarize_workload(obj)
                components[component_id(comp["kind"], comp["name"])] = comp
        for obj in kubectl_items("jobs", ns, errors):
            owners = [(o.get("kind"), o.get("name")) for o in obj.get("metadata", {}).get("ownerReferences") or []]
            cron = next((component_id("CronJob", n) for k, n in owners if k == "CronJob"), None)
            job_name = obj.get("metadata", {}).get("name")
            if cron and cron in components:
                status = obj.get("status", {})
                job_owner[f"{ns}/{job_name}"] = cron
                components[cron]["jobs"].append(
                    compact(
                        {
                            "name": job_name,
                            "active": status.get("active"),
                            "succeeded": status.get("succeeded"),
                            "failed": status.get("failed"),
                            "start_time": status.get("startTime"),
                            "completion_time": status.get("completionTime"),
                        }
                    )
                )
                continue
            comp = summarize_workload(obj)
            components[component_id("Job", job_name)] = comp
    for comp in components.values():
        if comp["kind"] == "CronJob":
            stuck = [j for j in comp["jobs"] if j.get("active") and not j.get("completion_time")]
            if len(stuck) > 1:
                comp["signals"].append(
                    f"{len(stuck)} jobs of this cronjob are active without completing (they pile up)"
                )
            comp["jobs"] = sorted(comp["jobs"], key=lambda j: j.get("start_time") or "", reverse=True)[:5]

    for ns in namespaces:
        for pod in kubectl_items("pods", ns, errors):
            summary = summarize_pod(pod)
            summary["_namespace"] = ns
            cid = component_for_pod(summary, components, job_owner)
            summary.pop("_namespace")
            summary.pop("owner")
            if cid is None:
                if summary["signals"] and len(unassigned_pods) < 10:
                    unassigned_pods.append({k: v for k, v in summary.items() if k != "labels"})
                continue
            summary.pop("labels")
            pods_by_component.setdefault(cid, []).append(summary)
            pod_to_component[summary["name"]] = cid
            pod_index[summary["name"]] = summary

    deploy_time = deployment_time(components)
    now = datetime.now(UTC)
    recent_changes: list[dict] = []
    for cid, comp in components.items():
        mod = parse_time(comp.get("modified"))
        if mod:
            comp["modified_seconds_ago"] = max(0, int((now - mod).total_seconds()))
        late = change_after_deploy(comp.get("modified"), deploy_time)
        if late is not None:
            comp["signals"].append(
                f"spec modified {comp.get('modified_seconds_ago')}s ago, {late}s after the application was deployed"
            )
            recent_changes.append({"object": cid, "modified": comp.get("modified"), "seconds_after_deploy": late})
    for cid, comp in components.items():
        comp["signals"].extend(replica_signals(comp))
        pods = pods_by_component.get(cid, [])
        comp["pods"] = pods
        for pod in pods:
            comp["signals"].extend(f"pod {pod['name']}: {s}" for s in pod["signals"])
            if pod.get("startup_notes"):
                comp.setdefault("startup_history", []).extend(f"pod {pod['name']}: {n}" for n in pod["startup_notes"])
        if comp["replicas"].get("desired", 0) and not pods:
            comp["signals"].append("no pods exist for this workload")

    constraints: list[dict] = []
    config_objects: dict = {}
    for ns in namespaces:
        attach_warning_events(kubectl_items("events", ns, errors), components, unassigned_events, job_owner, pod_index)
        dangling_services += attach_services(
            kubectl_items("services", ns, errors),
            kubectl_items("endpoints", ns, errors),
            components,
            deploy_time,
            recent_changes,
        )
        all_policies += attach_network_policies(kubectl_items("networkpolicies", ns, errors), components, deploy_time)
        attach_hpas(kubectl_items("horizontalpodautoscalers", ns, errors), components)
        attach_pvcs(kubectl_items("persistentvolumeclaims", ns, errors), components)
        constraints += summarize_namespace_constraints(
            kubectl_items("limitranges", ns, errors), kubectl_items("resourcequotas", ns, errors)
        )
        config_objects.update(
            summarize_config_objects(kubectl_items("secrets", ns, errors), kubectl_items("configmaps", ns, errors))
        )
    for comp in components.values():
        comp["healthy_hint"] = not comp["signals"]
    attach_namespace_constraints(constraints, components)
    attach_config_objects(config_objects, components, deploy_time)
    webhooks = summarize_webhooks(
        kubectl_items("mutatingwebhookconfigurations", None, errors, cluster_scoped=True)
        + kubectl_items("validatingwebhookconfigurations", None, errors, cluster_scoped=True),
        errors,
    )
    attach_webhooks(webhooks, components)
    cluster_dns = summarize_cluster_dns(errors, deploy_time)
    if cluster_dns.get("seconds_after_deploy"):
        recent_changes.append(
            {
                "object": "configmap/kube-system/coredns",
                "modified": cluster_dns.get("modified"),
                "seconds_after_deploy": cluster_dns["seconds_after_deploy"],
            }
        )
    for wh in webhooks:
        late = change_after_deploy(wh.get("created"), deploy_time)
        if late is not None:
            wh["seconds_after_deploy"] = late
            recent_changes.append(
                {
                    "object": f"{wh['kind']}/{wh['configuration']}",
                    "modified": wh.get("created"),
                    "seconds_after_deploy": late,
                }
            )
    for (kind, ns, name), obj in config_objects.items():
        late = change_after_deploy(obj.get("last_update"), deploy_time)
        if late is not None and any(
            name in (c.get("config_refs") or {}).get("secrets" if kind == "Secret" else "configmaps", [])
            for c in components.values()
            if c["namespace"] == ns
        ):
            recent_changes.append(
                {
                    "object": f"{kind.lower()}/{ns}/{name}",
                    "modified": obj.get("last_update"),
                    "seconds_after_deploy": late,
                }
            )
    for c in constraints:
        late = change_after_deploy(c.get("created"), deploy_time)
        if late is not None:
            c["seconds_after_deploy"] = late
            recent_changes.append(
                {
                    "object": f"{c['kind'].lower()}/{c['namespace']}/{c['name']}",
                    "modified": c.get("created"),
                    "seconds_after_deploy": late,
                }
            )
    for pol in all_policies:
        if pol.get("seconds_after_deploy"):
            recent_changes.append(
                {
                    "object": f"networkpolicy/{pol['namespace']}/{pol['name']}",
                    "modified": pol.get("created"),
                    "seconds_after_deploy": pol["seconds_after_deploy"],
                }
            )
    recent_changes.sort(key=lambda r: -(r.get("seconds_after_deploy") or 0))
    for ns in namespaces:
        try:
            top = run_kubectl(["top", "pods", "-n", ns, "--containers", "--no-headers"], timeout=KUBECTL_TIMEOUT)
            attach_resource_usage(top, components, pod_to_component)
        except Exception as exc:  # noqa: BLE001 - metrics-server is optional
            errors.append(f"kubectl top {ns}: {exc}")

    if include_logs:
        attach_logs(components, pods_by_component, errors, log_tail)

    alerts: list[dict] = []
    if alerts_fetcher is not None:
        try:
            alerts = parse_alerts(alerts_fetcher())
        except Exception as exc:  # noqa: BLE001 - alerts are optional evidence
            errors.append(f"alerts: {exc}")
        attach_alerts(alerts, components)

    rbac = summarize_rbac(namespaces, components, errors, deploy_time)
    for entry in rbac:
        if entry.get("seconds_after_deploy") is not None:
            recent_changes.append(
                {
                    "object": f"{entry['kind'].lower()}/{entry['namespace'] + '/' if entry.get('namespace') else ''}{entry['name']}",
                    "modified": entry.get("modified"),
                    "seconds_after_deploy": entry["seconds_after_deploy"],
                }
            )
    recent_changes.sort(key=lambda r: -(r.get("seconds_after_deploy") or 0))

    nodes = summarize_nodes(kubectl_items("nodes", None, errors))

    for cid in list(components):
        comp = components[cid]
        comp["signals"] = list(dict.fromkeys(comp["signals"]))
        comp["healthy"] = not comp["signals"] and not comp.get("log_error_lines")
        for key in ("selector", "template_labels", "healthy_hint"):
            comp.pop(key, None)
        for pod in comp["pods"]:
            pod.pop("signals", None)
            pod.pop("startup_notes", None)
            pod.pop("settled", None)
        for svc in comp["services"]:
            if not svc.get("matches_no_workload"):
                svc.pop("selector", None)
        components[cid] = compact(comp)
        components[cid]["healthy"] = comp["healthy"]

    cluster = {
        "namespaces": namespaces,
        "nodes": compact(nodes),
        "firing_alerts": alerts,
        "network_policies": [
            {k: v for k, v in p.items() if k not in ("ingress_rules", "egress_rules")} for p in all_policies
        ],
        "services_matching_no_workload": dangling_services,
        "application_deployed_at": deploy_time.isoformat() if deploy_time else None,
        "cluster_dns": cluster_dns,
        "recent_changes": recent_changes[:10],
        "namespace_constraints": compact(constraints),
        "admission_webhooks": compact(webhooks),
        "rbac": compact(rbac),
        "warning_events_not_attributed": unassigned_events,
        "unhealthy_pods_not_attributed": unassigned_pods,
    }
    app = {
        "name": app_info.get("app_name"),
        "description": trim(app_info.get("descriptions"), 600),
        "namespaces": namespaces,
    }
    snapshot = ClusterSnapshot(app=app, components=components, cluster=cluster, errors=errors)
    from clients.jev_diag.derive import post_process

    post_process(snapshot)
    logger.info(
        "Collected %d components (%d with structural signals, %d with only log errors), %d alerts, %d collection errors",
        len(components),
        sum(1 for c in components.values() if c.get("signals")),
        sum(1 for c in components.values() if not c.get("signals") and c.get("log_error_lines")),
        len(alerts),
        len(errors),
    )
    return snapshot


# --------------------------------------------------------------------------- budget


# Measured on a real run: 32.4k chars of compact JSON state billed as ~14.5k Jev input tokens,
# i.e. about 2.2 chars per token. Use 2.0 so the estimate errs high.
CHARS_PER_TOKEN = 2.0


def estimate_tokens(obj: object) -> int:
    """Conservative token estimate for JSON state (about 2 characters per token for compact JSON)."""
    return int(len(json.dumps(obj, separators=(",", ":"), default=str)) / CHARS_PER_TOKEN) + 1


def _cap(comp: dict, key: str, n: int) -> None:
    if isinstance(comp.get(key), list) and len(comp[key]) > n:
        comp[key] = comp[key][:n]


def _trim_steps() -> list[tuple[str, Callable[[dict], None]]]:
    """Ordered, increasingly aggressive reductions. Each mutates the state in place."""

    def for_components(fn: Callable[[dict], None], *, healthy_only: bool = False) -> Callable[[dict], None]:
        def apply(state: dict) -> None:
            for comp in state["components"].values():
                if healthy_only and not comp.get("healthy"):
                    continue
                fn(comp)

        return apply

    def drop_keys(*keys: str) -> Callable[[dict], None]:
        def fn(comp: dict) -> None:
            for k in keys:
                comp.pop(k, None)

        return fn

    def drop_container_keys(*keys: str) -> Callable[[dict], None]:
        def fn(comp: dict) -> None:
            for c in comp.get("containers", []):
                for k in keys:
                    c.pop(k, None)

        return fn

    def cap_previous_logs(n: int) -> Callable[[dict], None]:
        def fn(comp: dict) -> None:
            comp["previous_container_logs"] = {k: v[-n:] for k, v in comp.get("previous_container_logs", {}).items()}

        return fn

    def cap_cluster(key: str, n: int) -> Callable[[dict], None]:
        def apply(state: dict) -> None:
            if len(state["cluster"].get(key, [])) > n:
                state["cluster"][key] = state["cluster"][key][:n]

        return apply

    return [
        (
            "healthy: drop container command/args/env_from",
            for_components(drop_container_keys("command", "args", "env_from"), healthy_only=True),
        ),
        ("all: previous logs to 8 lines", for_components(cap_previous_logs(8))),
        ("all: log_signals to 4", for_components(lambda c: _cap(c, "log_signals", 4))),
        ("all: warning_events to 3", for_components(lambda c: _cap(c, "warning_events", 3))),
        (
            "healthy: drop pods/services/resource_usage/volumes/config",
            for_components(
                drop_keys(
                    "pods",
                    "services",
                    "resource_usage",
                    "volumes",
                    "config_refs",
                    "config_objects",
                    "jobs",
                    "startup_history",
                    "startup_events",
                    "log_findings",
                    "errors_point_to",
                ),
                healthy_only=True,
            ),
        ),
        ("healthy: drop env_names/ports", for_components(drop_container_keys("env_names", "ports"), healthy_only=True)),
        (
            "cluster: alerts to 15, unattributed events to 5",
            lambda s: (cap_cluster("firing_alerts", 15)(s), cap_cluster("warning_events_not_attributed", 5)(s)),
        ),
        ("all: drop container command/args", for_components(drop_container_keys("command", "args"))),
        (
            "all: log_signals to 2, events to 2",
            for_components(lambda c: (_cap(c, "log_signals", 2), _cap(c, "warning_events", 2))),
        ),
        ("healthy: drop log_signals", for_components(lambda c: c.pop("log_signals", None), healthy_only=True)),
        ("all: drop env_names", for_components(drop_container_keys("env_names"))),
        ("all: pods to 3", for_components(lambda c: _cap(c, "pods", 3))),
        ("all: previous logs to 3 lines", for_components(cap_previous_logs(3))),
        ("healthy: drop containers", for_components(drop_keys("containers"), healthy_only=True)),
        ("cluster: drop network policy rule bodies", lambda s: s["cluster"].pop("network_policies", None)),
        (
            "all: drop previous logs and resource_usage",
            for_components(drop_keys("previous_container_logs", "resource_usage")),
        ),
        (
            "all: pods to 1, drop container_states",
            for_components(
                lambda c: (_cap(c, "pods", 1), [p.pop("container_states", None) for p in c.get("pods", [])])
            ),
        ),
        ("all: drop log_signals", for_components(lambda c: c.pop("log_signals", None))),
    ]


def fit_state_to_budget(state: dict, max_tokens: int) -> tuple[dict, list[str]]:
    """Deep-copy `state` and apply trim steps in order until the estimate fits. Returns (state, applied)."""
    fitted = copy.deepcopy(state)
    applied: list[str] = []
    for label, step in _trim_steps():
        if estimate_tokens(fitted) <= max_tokens:
            break
        step(fitted)
        applied.append(label)
    return fitted, applied
