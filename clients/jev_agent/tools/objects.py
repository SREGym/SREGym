"""object_view and related_objects: single objects as they are now, and the objects around a workload."""

from __future__ import annotations

import logging
import re
from typing import Any

from clients.jev_agent.tools import _common
from clients.jev_agent.tools._common import CAUSELESS_REASONS, MESSAGE_CHARS, _comp, _components, _ends, _written
from clients.jev_agent.tools.config_changes import _env_refs
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag import changes as change_log
from clients.jev_diag import collector
from clients.jev_diag.checks import json_short, label_selector_matches

logger = logging.getLogger("all.jev_agent.tools")


RELATED_MAX = 10  # objects listed, abnormal ones first


def _failing_conditions(obj: dict) -> list[str]:
    """`type=status reason: message` of the conditions that report a problem: False for positive conditions (Ready,
    Available, Bound, AbleToScale, DisruptionAllowed, ...), True for the negative ones."""
    negative = {"ReplicaFailure", "ScalingLimited", "Failed", "Stalled", "Degraded"}
    out = []
    for c in (obj.get("status") or {}).get("conditions") or []:
        bad = c.get("status") == ("True" if c.get("type") in negative else "False")
        if bad and c.get("reason") not in CAUSELESS_REASONS:
            out.append(
                f"{c.get('type')}={c.get('status')} {c.get('reason') or ''}: {_ends(c.get('message') or '', 200)}"
            )
    return out


def _listed(resource: str, ns: str) -> list[dict]:
    """`kubectl get <resource>` items in the namespace, or [] when the type is absent or forbidden (logged)."""
    try:
        return _common._items(resource, ns)
    except Exception as exc:  # noqa: BLE001 - a resource type may be absent or forbidden
        logger.info("related_objects: %s in %s unreadable: %s", resource, ns, exc)
        return []


def _find_related(comp: dict, workload: dict) -> dict[tuple[str, str], tuple[str, dict]]:
    """{(kind, name): (why it is related, the object when listed)} for the objects around a workload, in the order
    they are looked up (owned ReplicaSets, volumes, env sources, ServiceAccount, role bindings, selectors, scalers)."""
    ns, kind, name = comp["namespace"], comp["kind"], comp["name"]
    tpl = (workload.get("spec") or {}).get("template") or {}
    labels = (tpl.get("metadata") or {}).get("labels") or {}
    pod_spec = tpl.get("spec") or {}
    found: dict[tuple[str, str], tuple[str, dict]] = {}  # (kind, name) -> (why, object when listed)

    def add(k: str, n: str | None, why: str, obj: dict | None = None) -> None:
        if n and (k, n) not in found:
            found[(k, n)] = (why, obj or {})

    for rs in _listed("replicasets", ns) if kind == "Deployment" else []:
        meta = rs.get("metadata") or {}
        owned = any(o.get("name") == name and o.get("kind") == kind for o in meta.get("ownerReferences") or [])
        if owned and ((rs.get("status") or {}).get("replicas") or (rs.get("spec") or {}).get("replicas")):
            add("ReplicaSet", meta.get("name"), "owned", rs)
    claims = {(c.get("metadata") or {}).get("name"): c for c in _listed("persistentvolumeclaims", ns)}
    for v in pod_spec.get("volumes") or []:
        claim = (v.get("persistentVolumeClaim") or {}).get("claimName")
        add("PersistentVolumeClaim", claim, f"volume {v.get('name')}", claims.get(claim))
        add("ConfigMap", (v.get("configMap") or {}).get("name"), f"volume {v.get('name')}")
        add("Secret", (v.get("secret") or {}).get("secretName"), f"volume {v.get('name')}")
    for kind_, nm in sorted(_env_refs(pod_spec)):
        add({"secret": "Secret", "configmap": "ConfigMap"}[kind_], nm, "environment")
    sa = pod_spec.get("serviceAccountName") or "default"
    add("ServiceAccount", sa, "runs as")
    for rb in _listed("rolebindings", ns):
        if any(x.get("kind") == "ServiceAccount" and x.get("name") == sa for x in rb.get("subjects") or []):
            ref = rb.get("roleRef") or {}
            add("RoleBinding", (rb.get("metadata") or {}).get("name"), f"grants {ref.get('kind')} {ref.get('name')}")
    for svc in _listed("services", ns):
        sel = (svc.get("spec") or {}).get("selector")
        if sel and all(labels.get(k) == v for k, v in sel.items()):
            add("Service", (svc.get("metadata") or {}).get("name"), "selects its pods", svc)
    for resource, k, field_ in (
        ("networkpolicies", "NetworkPolicy", "podSelector"),
        ("poddisruptionbudgets", "PodDisruptionBudget", "selector"),
    ):
        for obj in _listed(resource, ns):
            sel = (obj.get("spec") or {}).get(field_)
            if sel is not None and label_selector_matches(sel, labels):
                add(k, (obj.get("metadata") or {}).get("name"), "selects its pods", obj)
    for hpa in _listed("horizontalpodautoscalers", ns):
        ref = (hpa.get("spec") or {}).get("scaleTargetRef") or {}
        if ref.get("kind") == kind and ref.get("name") == name:
            add("HorizontalPodAutoscaler", (hpa.get("metadata") or {}).get("name"), "scales it", hpa)
    return found


def _related_rows(found: dict[tuple[str, str], tuple[str, dict]], ns: str) -> list[tuple[bool, str]]:
    """(abnormal, line) per related object: its state, failing conditions and latest Warning events."""
    warnings: dict[tuple[str, str], list[str]] = {}
    for ev in _listed("events", ns):
        obj = ev.get("involvedObject") or {}
        if ev.get("type") == "Warning":
            warnings.setdefault((obj.get("kind"), obj.get("name")), []).append(
                f"{ev.get('reason')} x{ev.get('count') or 1}: {_ends(ev.get('message') or '', 200)}"
            )
    rows: list[tuple[bool, str]] = []
    for (k, n), (why, obj) in found.items():
        st, spec = obj.get("status") or {}, obj.get("spec") or {}
        state = []
        if k == "PersistentVolumeClaim" and obj:
            state.append(f"{st.get('phase')}, modes {spec.get('accessModes')}")
        elif k == "ReplicaSet":
            state.append(f"{st.get('readyReplicas') or 0}/{spec.get('replicas')} ready")
        failing = _failing_conditions(obj)
        events = warnings.get((k, n)) or []
        text = f"{k} {n} ({why})" + (f": {'; '.join(state + failing)}" if state or failing else "")
        if events:
            text += " | Warning " + " | ".join(events[:2])
        rows.append((bool(failing or events), text))
    return rows


def run_related_objects(ctx: AgentContext, cid: str) -> ToolResult:
    """related_objects: every namespaced object that belongs to the workload, is used by it or acts on it (its
    ReplicaSets, claims, ConfigMaps, Secrets, ServiceAccount and role bindings, and the Services, network policies,
    disruption budgets and autoscalers that select or target it), each with its own failing conditions and Warning
    events. Facts about the objects around a workload that its pods do not show."""
    comp = _comp(ctx, cid)
    ns, kind, name = comp["namespace"], comp["kind"], comp["name"]
    workload = _common._get_json(["get", kind.lower(), name, "-n", ns, "-o", "json", "--show-managed-fields"])
    rows = _related_rows(_find_related(comp, workload), ns)
    rows.sort(key=lambda r: not r[0])
    return ToolResult(lines=[t for _, t in rows[:RELATED_MAX]] or [f"no related objects found for {cid}"])


OBJECT_SKIP_KEYS = {"metadata", "apiVersion", "kind"}
_ENCODED_RE = re.compile(r"^[A-Za-z0-9+/=_-]{120,}$")


def _elide_encoded(value: Any) -> Any:
    """The value with long base64 strings (CA bundles, certificates, keys) replaced by their length: their text says
    nothing to a reader, and it would crowd out the fields that do."""
    if isinstance(value, dict):
        return {k: _elide_encoded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_elide_encoded(v) for v in value]
    if isinstance(value, str) and _ENCODED_RE.match(value):
        return f"<{len(value)} chars of base64>"
    return value


def run_object_view(ctx: AgentContext, target: str) -> ToolResult:
    """Any non-workload object as it is now: when it was created and last written, then its data (ConfigMap),
    its key names only (Secret), or its spec/rules/status (anything else)."""
    parts = target.split("/")
    if len(parts) not in (2, 3):
        return ToolResult(
            lines=[f"object_view needs kind/namespace/name or kind/name, got {target}"], error="bad target"
        )
    kind, name = parts[0], parts[-1]
    args = ["get", kind, name] + (["-n", parts[1]] if len(parts) == 3 else []) + ["-o", "json", "--show-managed-fields"]
    obj = _common._get_json(args)
    meta = obj.get("metadata") or {}
    lines = [
        f"{obj.get('kind') or kind} {target.split('/', 1)[1]}: {_written(ctx, meta.get('creationTimestamp'), change_log.last_spec_write(meta))}"
    ]
    if kind.lower() in ("secret", "secrets"):
        lines.append(f"keys {sorted((obj.get('data') or {}).keys())} (values not read)")
    elif "data" in obj and isinstance(obj.get("data"), dict):
        for key, value in list(obj["data"].items())[:6]:
            lines.append(f"data {key}: " + collector.trim(str(value).replace("\n", " ⏎ "), MESSAGE_CHARS))
    else:
        for key, value in obj.items():
            if key in OBJECT_SKIP_KEYS or key == "status":
                continue
            text = json_short(_elide_encoded(value), 4 * MESSAGE_CHARS)
            for i in range(0, min(len(text), 4 * MESSAGE_CHARS), MESSAGE_CHARS):
                lines.append(f"{key}{' (cont.)' if i else ''}: {text[i : i + MESSAGE_CHARS]}")
        if obj.get("status"):
            lines.append("status: " + json_short(obj["status"], MESSAGE_CHARS))
    return ToolResult(lines=lines)


def object_targets(ctx: AgentContext) -> list[str]:
    """Objects worth viewing: the origin candidates the policy lists, then objects changed recently."""
    out = list(ctx.extra.get("origin_objects") or [])
    for change in ((ctx.overview or {}).get("cluster") or {}).get("recent_changes") or []:
        if isinstance(change, dict) and change.get("object"):
            out.append(change["object"])
    comps = _components(ctx)
    return [
        t
        for t in dict.fromkeys(out)
        if t not in comps and t.count("/") in (1, 2) and not t.startswith(("data/", "interaction/"))
    ]
