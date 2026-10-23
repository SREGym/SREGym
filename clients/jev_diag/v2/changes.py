"""What changed, read from the cluster's own records.

A change is judged against the object's own history or against the objects it acts on, never against an
assumed deployment time:

* a spec write made after the object was created (managedFields; status and scale writes excluded);
* an object created after the workloads it acts on already existed (a NetworkPolicy after the pods it selects,
  a webhook after the workloads it intercepts, a workload after the workloads it calls);
* a pod template that differs from the one current when the application was deployed (Deployment ReplicaSets,
  StatefulSet and DaemonSet ControllerRevisions), diffed field by field.

Each change names its writer (the field manager of the write that dates it), and writes by one writer within a few
seconds form a change set, the way kubectl and audit logs attribute a change.

Changes are then ranked: first those that act on an abnormal component, then the rest, each group by when it
happened relative to the start of the active symptom episode, which is estimated from the symptoms themselves
(errors that are still occurring, readiness loss, crash loops, Pending pods, current warning events, and firing
alerts). Routine platform writes (see `routine_reason`) are ranked last and summarized instead of listed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from clients.jev_diag.timeutil import now, parse_time, seconds_ago, seconds_between
from clients.jev_diag.v2.checks import describe_probe, json_short

MIN_CHANGE_SECONDS = 10  # written this long after creation, or created this long after its targets: a separate change
CHANGE_HORIZON_SECONDS = 3600  # changes older than this are not listed
ONSET_TOLERANCE_SECONDS = 10  # the onset estimate is coarse; a change this soon after it still counts as before
MAX_FIELD_PATHS = 10
MAX_TEMPLATE_CHANGES = 12
MAX_RECENT_CHANGES = 10
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"  # written by a rollout restart; no setting changes
POD_TEMPLATE_HASH = "pod-template-hash"  # the label a Deployment puts on each ReplicaSet's template; never a setting
DEPLOY_TOLERANCE_SECONDS = 5  # a change this close to the application's creation still counts as after it
SAME_CHANGE_SECONDS = 10  # writes by one writer this close together are one operation (a change set)
MAX_SET_MEMBERS = 8
# With Kubernetes' default probe timing (periodSeconds 10, failureThreshold 3) a failing liveness probe restarts a
# container about 30 s after it starts failing, and the kubelet re-emits a warning while its condition holds. A pod
# Ready this long without another crash, or an event series silent this long, has stopped.
RECOVERED_SECONDS = 60

_IGNORED_SUBRESOURCES = {"status", "scale"}
# Managers that write a workload's scale on their own (autoscalers); a scale write by anyone else is a change.
_SCALE_CONTROLLERS = re.compile(r"(?i)kube-controller-manager|autoscal|keda|vpa")

# --------------------------------------------------------------------------- managedFields


def spec_writes(meta: dict) -> list[dict]:
    """Writes to an object's spec: not status, and not scale unless someone other than an autoscaler made it."""
    return [
        f
        for f in meta.get("managedFields") or []
        if f.get("time")
        and (
            f.get("subresource") not in _IGNORED_SUBRESOURCES
            or (f.get("subresource") == "scale" and not _SCALE_CONTROLLERS.search(f.get("manager") or ""))
        )
    ]


def last_spec_write(meta: dict) -> str | None:
    times = [f["time"] for f in spec_writes(meta)]
    return max(times) if times else meta.get("creationTimestamp")


def modified_after_creation(meta: dict) -> int | None:
    """Seconds between creation and the last spec write, when the write came separately from the creation."""
    delta = seconds_between(parse_time(last_spec_write(meta)), parse_time(meta.get("creationTimestamp")))
    return delta if delta is not None and delta >= MIN_CHANGE_SECONDS else None


def created_after(created: str | None, targets_created: list[str | None]) -> int | None:
    """Seconds between the median creation time of the objects this one acts on and its own creation."""
    mine = parse_time(created)
    refs = sorted(t for t in (parse_time(x) for x in targets_created) if t)
    if mine is None or not refs:
        return None
    delta = seconds_between(mine, refs[len(refs) // 2])
    return delta if delta is not None and delta >= MIN_CHANGE_SECONDS else None


def _flatten(node: dict, prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, child in (node or {}).items():
        if key == ".":
            continue
        if key.startswith("f:"):
            path = f"{prefix}.{key[2:]}" if prefix else key[2:]
        elif key.startswith("k:"):
            try:
                item = json.loads(key[2:])
                label = item.get("name") or item.get("key") or ",".join(f"{k}={v}" for k, v in item.items())
            except (ValueError, AttributeError):
                label = key[2:]
            path = f"{prefix}[{label}]"
        elif key.startswith("v:"):
            path = f"{prefix}[{key[2:]}]"
        else:
            path = f"{prefix}.{key}" if prefix else key
        sub = {k: v for k, v in child.items() if k != "."} if isinstance(child, dict) else {}
        paths.extend(_flatten(sub, path) if sub else [path])
    return paths


def readable_path(path: str) -> str:
    for long, short in (
        ("spec.jobTemplate.spec.template.spec.", ""),
        ("spec.template.spec.", ""),
        ("spec.jobTemplate.spec.template.metadata.", "template."),
        ("spec.template.metadata.", "template."),
    ):
        if path.startswith(long):
            path = short + path[len(long) :]
            break
    return re.sub(r"(\[[^\]]+\])\.(?:name|value|valueFrom(?:\..*)?|key|containerPort|protocol)$", r"\1", path)


def later_writes(meta: dict) -> list[dict]:
    """Fields each writer other than the creator set after the object was created.

    managedFields records which writer owns which fields. The writer with the earliest recorded write is taken as
    the creator; any other writer that wrote at least MIN_CHANGE_SECONDS after creation owns the fields it
    changed. With a single writer the fields cannot be attributed, and nothing is returned.
    """
    created = parse_time(meta.get("creationTimestamp"))
    writes = spec_writes(meta)
    if created is None or len(writes) < 2:
        return []
    creator = min(writes, key=lambda f: f.get("time") or "")
    out = []
    for f in sorted(writes, key=lambda f: f.get("time") or ""):
        at = parse_time(f.get("time"))
        if f is creator or at is None or (at - created).total_seconds() < MIN_CHANGE_SECONDS:
            continue
        raw = [p for p in dict.fromkeys(_flatten(f.get("fieldsV1") or {})) if not p.startswith("metadata.")]
        paths = list(dict.fromkeys(readable_path(p) for p in raw))
        if paths:
            out.append({"at": f.get("time"), "fields": paths[:MAX_FIELD_PATHS], "raw": raw[:MAX_FIELD_PATHS]})
    return out


# Kubernetes' own components reconcile objects toward what others asked for; their writes are not changes.
_CORE_COMPONENTS = re.compile(
    r"^(?:kube-controller-manager|kube-scheduler|kubelet|kube-apiserver|cloud-controller-manager|before-first-apply)$"
)


def desired_state_writes(meta: dict) -> list[dict]:
    """Spec and data writes that change what an object asks for, whoever made them (a person, a script, a package
    manager, a GitOps controller): every spec write except those of Kubernetes' own components and of the object's
    own controller (a manager that also writes the object's status reconciles it)."""
    status_writers = {f.get("manager") for f in meta.get("managedFields") or [] if f.get("subresource") == "status"}
    return [
        w
        for w in spec_writes(meta)
        if not _CORE_COMPONENTS.match(w.get("manager") or "") and w.get("manager") not in status_writers
    ]


def change_writer(meta: dict, modified: bool) -> str | None:
    """The field manager of the write that dates a change: the latest desired-state write for a modification, the
    earliest (the creating one) for an object created late. None when no such write is recorded."""
    writes = sorted(desired_state_writes(meta), key=lambda f: f.get("time") or "")
    if not writes:
        return None
    return (writes[-1] if modified else writes[0]).get("manager")


# --------------------------------------------------------------------------- values of a change

MAX_CHANGED_VALUES = 4
VALUE_CHARS = 300
_MISSING = object()
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[([^\]]+)\]")
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
# Parts of an object that state what it does (its content when it was created after the application).
_CONTENT_KEYS = (
    "spec",
    "data",
    "rules",
    "webhooks",
    "subjects",
    "roleRef",
    "value",
    "globalDefault",
    "preemptionPolicy",
)


def value_at(obj: object, raw_path: str) -> object:
    """The value at a managedFields path (`spec.peerSelector`, `data.Corefile`, `spec.containers[app].env[KEY]`) in
    an object, list items found by their name or key; _MISSING when absent."""
    node = obj
    for field_name, label in _PATH_TOKEN.findall(raw_path):
        if field_name:
            if not isinstance(node, dict) or field_name not in node:
                return _MISSING
            node = node[field_name]
            continue
        if not isinstance(node, list):
            return _MISSING
        pairs = dict(kv.split("=", 1) for kv in label.split(",") if "=" in kv)
        node = next(
            (
                item
                for item in node
                if (
                    isinstance(item, dict)
                    and (
                        label in (str(item.get("name")), str(item.get("key")))
                        or (pairs and all(str(item.get(k)) == v for k, v in pairs.items()))
                    )
                )
                or str(item) == label
            ),
            _MISSING,
        )
        if node is _MISSING:
            return _MISSING
    return node


def changed_values(obj: dict, meta: dict) -> list[str]:
    """What the later writes set, as `path: old -> new` when the last applied configuration still holds the old value,
    else `path = new`. Secrets are never read."""
    if obj.get("kind") == "Secret":
        return []
    try:
        applied = json.loads((meta.get("annotations") or {}).get(_LAST_APPLIED) or "null")
    except ValueError:
        applied = None
    out = []
    for write in later_writes(meta):
        for raw in write.get("raw") or []:
            new = value_at(obj, raw)
            old = value_at(applied, raw) if applied else _MISSING
            path = readable_path(raw)
            if old is not _MISSING and old != new:
                if isinstance(old, str) and isinstance(new, str) and not (_encoded(old) or _encoded(new)):
                    # long text: where it differs
                    was, now_ = _shown(old, new, VALUE_CHARS)
                    out.append(f"{path}: {was} -> {now_}")
                else:
                    shown_new = "(removed)" if new is _MISSING else json_short(readable(new), VALUE_CHARS)
                    out.append(f"{path}: {json_short(readable(old), VALUE_CHARS)} -> {shown_new}")
            elif new is not _MISSING:
                out.append(f"{path} = {json_short(readable(new), VALUE_CHARS)}")
    return list(dict.fromkeys(out))[:MAX_CHANGED_VALUES]


_BASE64 = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")


def _encoded(text: str) -> bool:
    """A certificate (PEM) or a base64 payload: no readable setting in it."""
    if "-----BEGIN " in text:
        return True
    flat = text.replace("\n", "").replace("\r", "")
    return bool(_BASE64.fullmatch(flat)) and any(c.isdigit() for c in flat)


def readable(value):
    """`value` with encoded data (a CA bundle, a certificate, a base64 payload) replaced by its size: it carries no
    readable setting and would fill the space the settings need."""
    if isinstance(value, dict):
        return {k: readable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [readable(v) for v in value]
    if isinstance(value, str) and _encoded(value):
        return f"<{len(value)} characters of encoded data>"
    return value


def object_content(obj: dict) -> str | None:
    """What a new object does: its spec, data, rules or settings (key names only for a Secret)."""
    if obj.get("kind") == "Secret":
        keys = sorted((obj.get("data") or {}) | (obj.get("stringData") or {}))
        return f"keys {', '.join(keys)}" if keys else None
    content = {k: readable(obj[k]) for k in _CONTENT_KEYS if k in obj}
    return json_short(content, VALUE_CHARS * 2) if content else None


# --------------------------------------------------------------------------- pod template revisions


MAX_DRIFT_DIFFS = 4


def live_revision_drift(comp: dict, revisions: list[dict]) -> list[str]:
    """Pods that still run an older pod template than the workload's newest revision (a paused or stalled rollout, a
    partition, pods not yet replaced), each older template with how it differs from the newest one."""
    if len(revisions) < 2:
        return []
    newest = revisions[-1]
    by_key = {key: rev for rev in revisions for key in (rev.get("hash"), rev.get("name")) if key}
    stale: dict[int, list[str]] = {}
    for pod in comp.get("pods") or []:
        rev = by_key.get(pod.get("template_hash"))
        if rev is not None and rev["revision"] < newest["revision"]:
            stale.setdefault(rev["revision"], []).append(pod["name"])
    out = []
    for number, pods in sorted(stale.items()):
        old = next(r for r in revisions if r["revision"] == number)
        diff = [d for d in template_diff(old["template"], newest["template"]) if not d.startswith("pod label ")]
        differs = "; ".join(diff[:MAX_DRIFT_DIFFS]) or "no compared setting differs"
        out.append(
            f"{len(pods)} pod(s) ({', '.join(pods[:3])}) still run pod template revision {number}, not the newest "
            f"({newest['revision']}); revision {number} -> newest: {differs}"
        )
    return out


def revision_templates(replicasets: list[dict], controllerrevisions: list[dict]) -> dict:
    """(namespace, owner kind, owner name) -> revisions sorted oldest first, each with its pod template."""
    out: dict[tuple[str, str, str], list[dict]] = {}
    for rs in replicasets:
        meta = rs.get("metadata") or {}
        owner = next((o for o in meta.get("ownerReferences") or [] if o.get("kind") == "Deployment"), None)
        try:
            revision = int((meta.get("annotations") or {}).get("deployment.kubernetes.io/revision"))
        except (TypeError, ValueError):
            continue
        if owner is None:
            continue
        out.setdefault((meta.get("namespace"), "Deployment", owner.get("name")), []).append(
            {
                "revision": revision,
                "created": meta.get("creationTimestamp"),
                "template": (rs.get("spec") or {}).get("template") or {},
                "name": meta.get("name"),
                "hash": (meta.get("labels") or {}).get("pod-template-hash"),
            }
        )
    for cr in controllerrevisions:
        meta = cr.get("metadata") or {}
        owner = next(
            (o for o in meta.get("ownerReferences") or [] if o.get("kind") in ("StatefulSet", "DaemonSet")), None
        )
        if owner is None:
            continue
        template = ((cr.get("data") or {}).get("spec") or {}).get("template") or {}
        template = {k: v for k, v in template.items() if not k.startswith("$")}
        out.setdefault((meta.get("namespace"), owner.get("kind"), owner.get("name")), []).append(
            {
                "revision": int(cr.get("revision") or 0),
                "created": meta.get("creationTimestamp"),
                "template": template,
                "name": meta.get("name"),
                "hash": (meta.get("labels") or {}).get("controller-revision-hash"),
            }
        )
    for revs in out.values():
        revs.sort(key=lambda r: r["revision"])
    return out


ENV_VALUE_CHARS = 80


def _env_pairs(container: dict) -> list[tuple[str, str]]:
    """(name, value) for each env entry; values are compared whole and shortened only for display (_shown)."""
    pairs = []
    for e in container.get("env") or []:
        if "value" in e:
            desc = str(e.get("value"))
        else:
            vf = e.get("valueFrom") or {}
            ref = vf.get("secretKeyRef") or vf.get("configMapKeyRef") or vf.get("fieldRef") or {}
            source = next((k for k in ("secretKeyRef", "configMapKeyRef", "fieldRef") if vf.get(k)), "valueFrom")
            desc = f"from {source} {ref.get('name') or ref.get('fieldPath') or ''}{('.' + ref['key']) if ref.get('key') else ''}"
        pairs.append((e.get("name"), desc))
    return pairs


def _shown(old: str, new: str | None = None, width: int = ENV_VALUE_CHARS) -> tuple[str, str]:
    """Display forms of a text value (and of what it changed to): whole when short; otherwise a window starting just
    before the first difference, so a change late in a long value stays visible."""
    if new is None:
        return json_short(old, width), ""
    if max(len(old), len(new)) <= width:
        return old, new
    first = next((i for i, (a, b) in enumerate(zip(old, new, strict=False)) if a != b), min(len(old), len(new)))
    start = max(0, first - 20)

    def window(text: str) -> str:
        part = text[start : start + width]
        return ("…" if start else "") + part + ("…" if start + width < len(text) else "")

    return window(old), window(new)


def _containers(pod_spec: dict) -> dict[str, dict]:
    out = {}
    for group in ("initContainers", "containers"):
        for c in pod_spec.get(group) or []:
            out[c.get("name")] = {**c, "_group": group}
    return out


def template_diff(old: dict, new: dict) -> list[str]:
    """Field-level differences between two pod templates, old -> new."""
    changes: list[str] = []
    old_meta, new_meta = old.get("metadata") or {}, new.get("metadata") or {}
    for key, label in (("annotations", "template annotation"), ("labels", "pod label")):
        a, b = old_meta.get(key) or {}, new_meta.get(key) or {}
        for k in sorted(set(a) | set(b)):
            if a.get(k) == b.get(k) or k == POD_TEMPLATE_HASH:
                continue
            if k not in b:
                changes.append(f"{label} {k} removed")
            elif k not in a:
                changes.append(f"{label} {k} added: {json_short(b[k], 60)}")
            else:
                changes.append(f"{label} {k}: {json_short(a[k], 50)} -> {json_short(b[k], 50)}")
    old_spec, new_spec = old.get("spec") or {}, new.get("spec") or {}
    for key in (
        "nodeSelector",
        "affinity",
        "tolerations",
        "topologySpreadConstraints",
        "dnsPolicy",
        "dnsConfig",
        "hostAliases",
        "hostNetwork",
        "serviceAccountName",
        "restartPolicy",
        "securityContext",
        "priorityClassName",
        "runtimeClassName",
        "terminationGracePeriodSeconds",
    ):
        if old_spec.get(key) != new_spec.get(key):
            changes.append(f"pod {key}: {json_short(old_spec.get(key), 60)} -> {json_short(new_spec.get(key), 60)}")
    old_vols = {v.get("name"): v for v in old_spec.get("volumes") or []}
    new_vols = {v.get("name"): v for v in new_spec.get("volumes") or []}
    for name in sorted(set(old_vols) | set(new_vols)):
        if old_vols.get(name) == new_vols.get(name):
            continue
        if name not in new_vols:
            changes.append(f"volume {name} removed")
        elif name not in old_vols:
            changes.append(
                f"volume {name} added: {json_short({k: v for k, v in new_vols[name].items() if k != 'name'}, 80)}"
            )
        else:
            changes.append(
                f"volume {name} changed: {json_short({k: v for k, v in new_vols[name].items() if k != 'name'}, 80)}"
            )
    old_c, new_c = _containers(old_spec), _containers(new_spec)
    for name in sorted(set(old_c) | set(new_c)):
        if name not in old_c:
            changes.append(f"container {name} added ({new_c[name]['_group']}, image {new_c[name].get('image')})")
            continue
        if name not in new_c:
            changes.append(f"container {name} removed")
            continue
        a, b = old_c[name], new_c[name]
        for key in ("image", "command", "args", "workingDir", "restartPolicy", "securityContext", "lifecycle"):
            if a.get(key) != b.get(key):
                # command and args carry whole programs and filter expressions: keep the new value readable
                wide = key in ("command", "args")
                changes.append(
                    f"container {name} {key}: {json_short(a.get(key), 120 if wide else 60)} -> "
                    f"{json_short(b.get(key), 500 if wide else 60)}"
                )
        env_a, env_b = _env_pairs(a), _env_pairs(b)
        map_a, map_b = dict(env_a), dict(env_b)  # a duplicated name resolves to its last value, as in Kubernetes
        for k in sorted(set(map_a) | set(map_b)):
            if k not in map_a:
                changes.append(f"container {name} env {k} added: {_shown(map_b[k])[0]}")
            elif k not in map_b:
                changes.append(f"container {name} env {k} removed (was {_shown(map_a[k])[0]})")
            elif map_a[k] != map_b[k]:
                was, now_ = _shown(map_a[k], map_b[k])
                changes.append(f"container {name} env {k}: {was} -> {now_}")
        names_a, names_b = [k for k, _ in env_a], [k for k, _ in env_b]
        for k in sorted({k for k in names_b if names_b.count(k) > 1} - {k for k in names_a if names_a.count(k) > 1}):
            values = [_shown(v)[0] for n, v in env_b if n == k]
            changes.append(f"container {name} env {k} is now defined {len(values)} times: {', '.join(values)}")
        if (a.get("envFrom") or []) != (b.get("envFrom") or []):
            changes.append(
                f"container {name} envFrom: {json_short(a.get('envFrom'), 60)} -> {json_short(b.get('envFrom'), 60)}"
            )
        res_a, res_b = a.get("resources") or {}, b.get("resources") or {}
        for part in ("limits", "requests"):
            pa, pb = res_a.get(part) or {}, res_b.get(part) or {}
            for res in sorted(set(pa) | set(pb)):
                if pa.get(res) != pb.get(res):
                    changes.append(
                        f"container {name} {part}.{res}: {pa.get(res) or 'unset'} -> {pb.get(res) or 'unset'}"
                    )
        for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
            if a.get(probe) != b.get(probe):
                changes.append(
                    f"container {name} {probe}: {describe_probe(a.get(probe))} -> {describe_probe(b.get(probe))}"
                )
        ports_a = [p.get("containerPort") for p in a.get("ports") or []]
        ports_b = [p.get("containerPort") for p in b.get("ports") or []]
        if ports_a != ports_b:
            changes.append(f"container {name} ports: {ports_a} -> {ports_b}")
        mounts_a = {(m.get("name"), m.get("mountPath"), m.get("subPath")) for m in a.get("volumeMounts") or []}
        mounts_b = {(m.get("name"), m.get("mountPath"), m.get("subPath")) for m in b.get("volumeMounts") or []}
        for m in sorted(mounts_b - mounts_a, key=str):
            changes.append(f"container {name} mounts volume {m[0]} at {m[1]}" + (f" (subPath {m[2]})" if m[2] else ""))
        for m in sorted(mounts_a - mounts_b, key=str):
            changes.append(f"container {name} no longer mounts volume {m[0]} at {m[1]}")
    return changes[:MAX_TEMPLATE_CHANGES]


def restart_only(diffs: list[str]) -> bool:
    """Whether a pod template diff is only a rollout restart (the restart annotation), with no setting changed."""
    return bool(diffs) and all(RESTART_ANNOTATION in d for d in diffs)


def deploy_revision(revs: list[dict], deployed: datetime | None) -> dict:
    """The revision current when the application was deployed: the newest one created by MIN_CHANGE_SECONDS after
    the deploy time (a write that close is part of deploying), else the oldest one kept. A workload created later,
    for example deleted and re-created by the fault, is compared with its first revision."""
    if deployed is None:
        return revs[0]
    cutoff = deployed + timedelta(seconds=MIN_CHANGE_SECONDS)
    deployed_revs = [r for r in revs if (at := parse_time(r.get("created"))) and at <= cutoff]
    return deployed_revs[-1] if deployed_revs else revs[0]


def attach_template_changes(components: dict[str, dict], revisions: dict, deployed: datetime | None = None) -> None:
    """For workloads with revision history, how the current pod template differs from the one current when the
    application was deployed. A setting changed and then restarted therefore still shows its old and new value."""
    for comp in components.values():
        revs = revisions.get((comp["namespace"], comp["kind"], comp["name"])) or []
        if len(revs) < 2:
            continue
        previous, current = deploy_revision(revs, deployed), revs[-1]
        diff = template_diff(previous["template"], current["template"])
        if diff:
            comp["template_changes"] = {
                "revision": current["revision"],
                "changed_at": current["created"],
                "changes": diff,
                "restart_only": restart_only(diff),
            }


# --------------------------------------------------------------------------- change records


def object_change(
    obj_id: str,
    kind: str,
    name: str,
    namespace: str | None,
    meta: dict,
    *,
    affects: list[str] | None = None,
    targets_created: list[str | None] | None = None,
    extra: dict | None = None,
    revised_at: str | None = None,
    obj: dict | None = None,
) -> dict | None:
    """A change record for one object, or None when the object shows no separate recent write.

    `revised_at` is when the workload's current pod template revision was created: a revision diff is a change
    even when the object's write history (managedFields) records no separate spec write, e.g. a field removed.
    With the object itself (`obj`), the record also says what changed: the values the later writes set (old -> new
    where the last applied configuration keeps the old one), or, for an object created late, what it contains.
    """
    created = meta.get("creationTimestamp")
    modified = modified_after_creation(meta)
    late = created_after(created, targets_created or [])
    if revised_at:  # like any write, a revision this close to creation is part of deploying the object
        apart = seconds_between(parse_time(revised_at), parse_time(created))
        revised_at = revised_at if apart is not None and apart >= MIN_CHANGE_SECONDS else None
    if modified is None and late is None and not revised_at:
        return None
    if modified is not None:
        at = last_spec_write(meta)
    elif late is not None:
        at = created
    else:
        at = revised_at
    age = seconds_ago(at)
    if age is None or age > CHANGE_HORIZON_SECONDS:
        return None
    record = {
        "object": obj_id,
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "created": created,
        "at": at,
        "affects": sorted(set(affects or [])),
        # a revision diff alone records no write of its own: its writer is unknown
        "writer": change_writer(meta, modified is not None) if modified is not None or late is not None else None,
    }
    if modified is not None:
        record["modified_after_creation_s"] = modified
        fields = [f for w in later_writes(meta) for f in w["fields"]]
        if fields:
            record["fields"] = list(dict.fromkeys(fields))[:MAX_FIELD_PATHS]
    if late is not None:
        record["created_after_targets_s"] = late
    if obj is not None:
        values = changed_values(obj, meta) if modified is not None else []
        content = object_content(obj) if modified is None and late is not None else None
        if values:
            record["values"] = values
        if content:
            record["content"] = content
    if modified is None and late is None:
        record["revision_changed"] = True
    if extra:
        record.update(extra)
    return record


# --------------------------------------------------------------------------- symptom onset and ranking


def estimate_symptom_onset(
    components: dict[str, dict], cluster: dict, collected_at: datetime
) -> tuple[datetime | None, str | None]:
    """The start of the active symptom episode: the earliest start among abnormalities that are still active, outside
    telemetry components.

    Still active means: a pod that is not ready or Pending; an error-log signature still occurring (failures to
    export telemetry excluded: they never break a request); a container crash, or a warning event about a pod, while
    that pod has not stayed Ready for RECOVERED_SECONDS since; any other warning event series that recurred within
    RECOVERED_SECONDS. A liveness-killed pod is Ready only between kills, so readiness counts only once it has lasted.
    What ended before is an earlier episode, typically warm-up restarts of components that waited for their
    dependencies and have been Ready since, or events about pods that were replaced; it does not date this one.

    Nothing dated before the application was deployed counts, and an alert counts only when its namespace label is
    one of the application's: the onset is the application's, and alerts about nodes or other namespaces can begin
    and end independently of it.
    """
    candidates: list[tuple[datetime, str]] = []
    deployed = application_created(components)
    namespaces = {c["namespace"] for c in components.values()}

    def recovered(pod: dict) -> bool:
        since = parse_time(pod.get("ready_since"))
        ready, _, total = (pod.get("ready") or "0/1").partition("/")
        return (
            since is not None
            and ready == total
            and pod.get("phase") == "Running"
            and (collected_at - since).total_seconds() >= RECOVERED_SECONDS
        )

    for cid, comp in components.items():
        if comp.get("role") == "observability":
            continue
        for sig in comp.get("log_signals") or []:
            started = sig.get("episode_start_seconds_ago", sig.get("first_seen_seconds_ago"))
            if sig.get("state") == "ongoing" and started is not None and not sig.get("_telemetry"):
                candidates.append((collected_at - timedelta(seconds=started), f"errors in {cid}"))
        pods = {pod.get("name"): pod for pod in comp.get("pods") or []}
        for pod in pods.values():
            moment = parse_time(pod.get("not_ready_since"))
            if moment and pod.get("phase") != "Succeeded":  # a completed pod is not ready by design
                candidates.append((moment, f"pod not ready in {cid}"))
            moment = parse_time(pod.get("last_crash_at"))
            if moment and not recovered(pod):
                candidates.append((moment, f"container crash in {cid}"))
            if pod.get("phase") == "Pending" and parse_time(pod.get("created")):
                candidates.append((parse_time(pod["created"]), f"pod pending in {cid}"))
        for ev in comp.get("warning_events") or []:
            first = parse_time(ev.get("first_seen"))
            last = parse_time(ev.get("last_seen")) or first
            obj = ev.get("object") or ""
            pod = pods.get(obj[4:]) if obj.startswith("pod/") else None
            recurring = last is not None and (collected_at - last).total_seconds() < RECOVERED_SECONDS
            if first and (recurring or (pod is not None and not recovered(pod))):
                candidates.append((first, f"{ev.get('reason')} events on {cid}"))
    for alert in cluster.get("firing_alerts") or []:
        moment = parse_time(alert.get("active_since"))
        if moment and (alert.get("labels") or {}).get("namespace") in namespaces:
            candidates.append((moment, f"alert {alert.get('alertname')}"))
    candidates = [c for c in candidates if deployed is None or c[0] >= deployed]
    if not candidates:
        return None, None
    moment, source = min(candidates, key=lambda c: c[0])
    return moment, source


def _acts_on(kind: str) -> str:
    return {
        "Service": "it selects",
        "NetworkPolicy": "it selects",
        "ConfigMap": "that read it",
        "Secret": "that read it",
        "MutatingWebhookConfiguration": "it intercepts",
        "ValidatingWebhookConfiguration": "it intercepts",
        "ResourceQuota": "in its namespace",
        "LimitRange": "in its namespace",
        "Role": "bound to it",
        "ClusterRole": "bound to it",
        "PersistentVolume": "that mount its claim",
    }.get(kind, "it calls")


def relative_to_symptoms(before: int | None) -> str:
    if before is None:
        return ""
    if before >= 0:
        return f"; {before}s before the first active symptom"
    if before >= -ONSET_TOLERANCE_SECONDS:  # finer than the onset estimate can tell
        return "; at the first active symptom"
    return f"; {-before}s after the first active symptom"


def workload_runs(comp: dict) -> str | None:
    """What a workload runs: each container's command, args and image (without its digest)."""
    parts = []
    for c in comp.get("containers") or []:
        what = []
        if c.get("command"):
            what.append(f"command {' '.join(map(str, c['command']))}")
        if c.get("args"):
            what.append(f"args {' '.join(map(str, c['args']))}")
        what.append(f"image {(c.get('image') or '').split('@')[0]}")
        parts.append(f"container {c.get('name')}{' (init)' if c.get('init') else ''}: {', '.join(what)}")
    return json_short("; ".join(parts), VALUE_CHARS) if parts else None


MAX_SET_NAMED = 4  # objects of a change set named in a change's signal


def written_by(change: dict) -> str:
    """ "; written by W" and, for a change set, the other objects W wrote in the same operation."""
    if not change.get("writer"):
        return ""
    members = change.get("set_members") or []
    if not members:
        return f"; written by {change['writer']}"
    more = (change.get("set_size") or len(members) + 1) - 1 - min(len(members), MAX_SET_NAMED)
    named = ", ".join(members[:MAX_SET_NAMED]) + (f" and {more} more" if more > 0 else "")
    return f"; written by {change['writer']} in one operation with {named}"


def describe_change(change: dict, component_id: str | None = None) -> str:
    """Signal text for a change, from the point of view of `component_id`, ending with who wrote it and what else that
    writer wrote in the same operation: a value written into a workload by an automation object created alongside
    points at that object."""
    ago = change.get("seconds_ago")
    rel = relative_to_symptoms(change.get("seconds_before_symptoms")) + written_by(change)
    fields = f" (fields: {', '.join(change['fields'][:4])})" if change.get("fields") else ""
    template = ""
    if change.get("template_changes"):
        template = f"; pod template changes: {'; '.join(change['template_changes'][:3])}"
    if change["object"] == component_id and change.get("revision_changed"):
        return f"pod template changed {ago}s ago{template}{rel}"
    if change["object"] == component_id:
        if change.get("modified_after_creation_s") is not None:
            own = f"; now {'; '.join(change['values'])}" if change.get("values") else ""
            return (
                f"spec modified {ago}s ago, {change['modified_after_creation_s']}s after this object was created"
                f"{fields}{own}{template}{rel}"
            )
        runs = f"; it runs {change['content']}" if change.get("content") else ""
        return f"created {ago}s ago, {change['created_after_targets_s']}s after the workloads it calls{runs}{rel}"
    what = f"{change['kind']} {change['name']}"
    values = f"; now {'; '.join(change['values'])}" if change.get("values") else ""
    content = f"; it holds {change['content']}" if change.get("content") else ""
    if change.get("modified_after_creation_s") is not None:
        return (
            f"{what} was modified {ago}s ago, {change['modified_after_creation_s']}s after it was created{fields}"
            f"{values}{rel}"
        )
    return (
        f"{what} was created {ago}s ago, {change['created_after_targets_s']}s after the workloads "
        f"{_acts_on(change['kind'])}{content}{rel}"
    )


def application_created(components: dict[str, dict]) -> datetime | None:
    """When the application was deployed: the earliest creation time among its own workloads (a related
    namespace's workloads may be older)."""
    times = [parse_time(c.get("created")) for c in components.values() if not c.get("related")]
    times = [t for t in times if t]
    return min(times) if times else None


def before_deploy(change: dict, deployed: datetime | None) -> bool:
    """Whether a change was made before the application's workloads existed: such a write is part of the
    environment the application was deployed into, not a change to the running application."""
    at = parse_time(change.get("at"))
    gap = seconds_between(deployed, at) if deployed and at else None
    return gap is not None and gap > DEPLOY_TOLERANCE_SECONDS


def routine_reason(change: dict) -> str | None:
    """Why a change is routine platform activity rather than a change to what the application is or runs, or None.

    Writes the cluster's own records attribute to a controller carry `routine` from collection (a volume provisioned
    for a claim, an object maintained by its owner's controller, a background writer's bulk bookkeeping; see
    outside.change_radar). A rollout restart changes no setting, and a telemetry object carries no application
    request: both are context, not candidates.
    """
    if change.get("routine"):
        return change["routine"]
    if change.get("restart_only"):
        return "rollout restart, no setting changed"
    if change.get("role") == "observability":
        return "telemetry object"
    return None


def assign_change_sets(changes: list[dict]) -> None:
    """Group changes into change sets: writes by one writer, each within SAME_CHANGE_SECONDS of the previous one, are
    one operation (an apply of several manifests, one script, one release). Each member of a set of two or more
    objects gets `change_set` (sets numbered by their first write), `set_members` (the other members' objects, at
    most MAX_SET_MEMBERS) and `set_size`. Routine changes never join a set: a background writer's bookkeeping would
    tie a real change to dozens of unrelated objects. A change with no known writer joins none."""
    by_writer: dict[str, list[tuple[datetime, dict]]] = {}
    for change in changes:
        at = parse_time(change.get("at"))
        if change.get("writer") and at and not change.get("routine"):
            by_writer.setdefault(change["writer"], []).append((at, change))
    sets: list[list[tuple[datetime, dict]]] = []
    for writes in by_writer.values():
        writes.sort(key=lambda w: w[0])
        sets.append([writes[0]])
        for previous, write in zip(writes, writes[1:], strict=False):
            if (write[0] - previous[0]).total_seconds() <= SAME_CHANGE_SECONDS:
                sets[-1].append(write)
            else:
                sets.append([write])
    sets = sorted((s for s in sets if len({c["object"] for _, c in s}) > 1), key=lambda s: (s[0][0], s[0][1]["object"]))
    for number, members in enumerate(sets, 1):
        objects = list(dict.fromkeys(c["object"] for _, c in members))
        for _, change in members:
            change["change_set"] = f"cs{number}"
            change["set_members"] = [o for o in objects if o != change["object"]][:MAX_SET_MEMBERS]
            change["set_size"] = len(objects)


_LISTED_KEYS = (
    "object",
    "kind",
    "affects",
    "at",
    "seconds_ago",
    "seconds_before_symptoms",
    "fields",
    "values",  # what the change wrote, so triage reads the value, not only the write
    "content",
    "writer",
    "change_set",
    "set_members",
    "set_size",
)


def rank_changes(components: dict[str, dict], cluster: dict, collected_at: datetime | None = None) -> list[dict]:
    """Order recorded changes and attach each one that is not routine to the components it acts on, as a signal.
    Returns the full ranked list.

    Changes that act on an abnormal component (one with any signal or error logs) come first, then the rest: an
    object that acts on no component in particular (a cluster-level setting, a node, a volume) can still be the
    cause, so it is ranked lower, never dropped. Within each group, changes made before the first active symptom come
    first, nearest first; then the rest, most recent first (most recent first throughout without an onset
    estimate). Routine changes (`routine_reason`) come last and stay out of `recent_changes`: cluster_findings
    summarizes them in a line per writer and kind.
    """
    collected_at = collected_at or now()
    onset, source = estimate_symptom_onset(components, cluster, collected_at)
    cluster["symptom_onset"] = onset.isoformat(timespec="seconds") if onset else None
    cluster["symptom_onset_source"] = source
    abnormal = {cid for cid, comp in components.items() if comp.get("signals") or comp.get("log_error_lines")}
    ranked = []
    deployed = application_created(components)
    for raw in cluster.get("_changes") or []:
        change = dict(raw)
        if before_deploy(change, deployed):
            continue
        at = parse_time(change.get("at"))
        change["seconds_ago"] = seconds_ago(at)
        change["seconds_before_symptoms"] = seconds_between(onset, at) if onset else None
        affected = [components[c] for c in change.get("affects") or [] if c in components]
        if affected and all(c.get("role") == "observability" for c in affected):
            change["role"] = "observability"
        change["routine"] = routine_reason(change)
        ranked.append(change)

    def timing(change: dict) -> tuple:
        before = change.get("seconds_before_symptoms")
        if before is None:
            return (1, change.get("seconds_ago") or 0)
        if before >= -ONSET_TOLERANCE_SECONDS:
            return (0, before)
        return (2, change.get("seconds_ago") or 0)

    ranked.sort(key=lambda c: (bool(c["routine"]), not (abnormal & set(c.get("affects") or [])), *timing(c)))
    assign_change_sets(ranked)
    listed = [c for c in ranked if not c["routine"]]
    cluster["recent_changes"] = [
        {k: v for k, v in c.items() if (k in _LISTED_KEYS or k.endswith("_s")) and v is not None}
        for c in listed[:MAX_RECENT_CHANGES]
    ]
    flag = "nearest_change_before_symptoms" if onset else "most_recent_change"
    first = next((c for c in listed if not onset or timing(c)[0] == 0), None)
    for change in listed:  # a routine change is context for the cluster, not evidence about a component
        for cid in change.get("affects") or []:
            comp = components.get(cid)
            if comp is None:
                continue
            text = describe_change(change, cid)
            if text not in comp["signals"]:
                comp["signals"].append(text)
            if change["object"] == cid and change.get("modified_after_creation_s") is not None:
                comp["spec_changed_after_creation_s"] = change["modified_after_creation_s"]
            if change is first:
                comp[flag] = True
    return ranked
