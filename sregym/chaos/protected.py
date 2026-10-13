"""Protected components that a fault may not touch.

The workload generator, the telemetry pipeline and alerting are what measure
the SLO and what the agent observes the fault through; a fault that edits them
would fake its own impact or blind the agent. ``digest`` fingerprints their
desired state (spec/data, not status) so the integrity gate can compare the
cluster before and after injection.
"""

from __future__ import annotations

import hashlib
import json
import subprocess

OBSERVABILITY_NAMESPACE = "observe"
OBSERVABILITY_KINDS = "deployments,statefulsets,daemonsets,configmaps,services"
# Labels the workload generators carry in the application namespace
# (see sregym/service/agent_visibility_policy.py).
WORKLOAD_SELECTORS = ("job=workload", "app=locust-fetcher", "app=load-generator")

_VOLATILE_METADATA = (
    "resourceVersion",
    "uid",
    "generation",
    "creationTimestamp",
    "managedFields",
    "selfLink",
    "ownerReferences",
)
_VOLATILE_ANNOTATIONS = (
    "deployment.kubernetes.io/revision",
    "kubectl.kubernetes.io/last-applied-configuration",
    "kubectl.kubernetes.io/restartedAt",
    "control-plane.alpha.kubernetes.io/leader",
)


def _kubectl_json(args: list[str]) -> dict:
    out = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return {"items": [], "error": out.stderr.strip()[:300]}
    return json.loads(out.stdout)


def _normalize(obj: dict) -> dict:
    meta = obj.get("metadata", {})
    for key in _VOLATILE_METADATA:
        meta.pop(key, None)
    annotations = meta.get("annotations") or {}
    for key in _VOLATILE_ANNOTATIONS:
        annotations.pop(key, None)
    obj.pop("status", None)
    spec = obj.get("spec") or {}
    # Job selectors and pod template labels embed the generated controller uid.
    if obj.get("kind") == "Job":
        spec.pop("selector", None)
        template_labels = spec.get("template", {}).get("metadata", {}).get("labels", {})
        for key in list(template_labels):
            if "controller-uid" in key or key in ("job-name", "batch.kubernetes.io/job-name"):
                template_labels.pop(key)
        spec.pop("suspend", None)
    # Coordination ConfigMaps (leader election) change on their own.
    if obj.get("kind") == "ConfigMap" and meta.get("name", "").endswith(("-leader", "-lock")):
        obj.pop("data", None)
    return obj


def snapshot(app_namespace: str) -> dict[str, str]:
    """Return {resource key: sha256 of normalized desired state}."""
    objects = []
    objects += _kubectl_json(["get", OBSERVABILITY_KINDS, "-n", OBSERVABILITY_NAMESPACE]).get("items", [])
    for selector in WORKLOAD_SELECTORS:
        objects += _kubectl_json(["get", "jobs,deployments,configmaps", "-n", app_namespace, "-l", selector]).get(
            "items", []
        )
    single = _kubectl_json(["get", "configmap", "wrk2-payload-script", "-n", app_namespace])
    if single.get("kind") == "ConfigMap":
        objects.append(single)
    out = {}
    for obj in objects:
        meta = obj.get("metadata", {})
        key = f"{obj.get('kind')}/{meta.get('namespace')}/{meta.get('name')}"
        body = json.dumps(_normalize(obj), sort_keys=True)
        out[key] = hashlib.sha256(body.encode()).hexdigest()
    return out


def workload_running(app_namespace: str) -> bool:
    pods = _kubectl_json(["get", "pods", "-n", app_namespace, "-l", "job=workload"]).get("items", [])
    pods += _kubectl_json(["get", "pods", "-n", app_namespace, "-l", "job-name=wrk2-job"]).get("items", [])
    return any(p.get("status", {}).get("phase") == "Running" for p in pods)


def compare(before: dict[str, str], after: dict[str, str]) -> dict:
    changed = sorted(k for k in before if k in after and before[k] != after[k])
    removed = sorted(k for k in before if k not in after)
    added = sorted(k for k in after if k not in before)
    return {"intact": not (changed or removed), "changed": changed, "removed": removed, "added": added}


LEAK_KINDS = (
    "deployments,statefulsets,daemonsets,replicasets,pods,services,configmaps,secrets,serviceaccounts,"
    "networkpolicies,roles,rolebindings,jobs,cronjobs,horizontalpodautoscalers,poddisruptionbudgets,"
    "persistentvolumeclaims,ingresses,endpoints"
)


def _strings(obj, path=""):
    """Yield (path, text) for every key and string value in a JSON object."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield f"{path}.{key}", str(key)
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _strings(value, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def leak_scan(namespace: str) -> dict[str, str]:
    """Fixer-visible strings in the namespace that would reveal an injected fault, mapped to where.

    Callers diff the keys (the text itself) against a pre-injection scan, so
    strings that existed before (e.g. image registries) never count, even on
    objects that were recreated under new names.
    """
    import re

    from sregym.chaos.artifact import SUSPICIOUS_WORDS

    # Every app image lives under ghcr.io/sregym/, so the project name is no signal here.
    suspicious = re.compile(r"(?<![a-z])(" + "|".join(w for w in SUSPICIOUS_WORDS if w != "sregym") + ")", re.I)
    found: dict[str, str] = {}
    for obj in _kubectl_json(["get", LEAK_KINDS, "-n", namespace]).get("items", []):
        meta = obj.get("metadata", {})
        meta.pop("managedFields", None)
        if obj.get("kind") == "Secret":
            obj.pop("data", None)  # values are base64; names and labels still count
        where = f"{obj.get('kind')}/{meta.get('name')}"
        for path, text in _strings({"metadata": meta, "spec": obj.get("spec"), "data": obj.get("data")}):
            if suspicious.search(text):
                found.setdefault(text[:200], f"{where}{path}")
    for event in _kubectl_json(["get", "events", "-n", namespace]).get("items", []):
        text = f"{event.get('reason', '')} {event.get('message', '')}"
        if suspicious.search(text):
            found.setdefault(text[:200], "Event")
    return found
