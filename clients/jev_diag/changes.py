"""What changed, read from the cluster's own records.

A change is judged against the object's own history or against the objects it acts on, never against an
assumed deployment time:

* a spec write made after the object was created (managedFields; status and scale writes excluded);
* an object created after the workloads it acts on already existed (a NetworkPolicy after the pods it selects,
  a webhook after the workloads it intercepts, a workload after the workloads it calls);
* a pod template that differs from the previous revision (Deployment ReplicaSets, StatefulSet and DaemonSet
  ControllerRevisions), diffed field by field.

Changes are then ranked by when they happened relative to the first active symptom, which is estimated from the
symptoms themselves: errors that are still occurring, readiness loss, crash loops, Pending pods, current warning
events, and firing alerts.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from clients.jev_diag.checks import describe_probe, json_short
from clients.jev_diag.timeutil import now, parse_time, seconds_ago, seconds_between

MIN_CHANGE_SECONDS = 10  # written this long after creation, or created this long after its targets: a separate change
CHANGE_HORIZON_SECONDS = 3600  # changes older than this are not listed
ONSET_TOLERANCE_SECONDS = 10  # the onset estimate is coarse; a change this soon after it still counts as before
MAX_FIELD_PATHS = 10
MAX_TEMPLATE_CHANGES = 12
MAX_RECENT_CHANGES = 10

_IGNORED_SUBRESOURCES = {"status", "scale"}

# --------------------------------------------------------------------------- managedFields


def spec_writes(meta: dict) -> list[dict]:
    return [
        f
        for f in meta.get("managedFields") or []
        if f.get("time") and f.get("subresource") not in _IGNORED_SUBRESOURCES
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
        paths = [readable_path(p) for p in _flatten(f.get("fieldsV1") or {})]
        paths = [p for p in dict.fromkeys(paths) if not p.startswith("metadata.")]
        if paths:
            out.append({"at": f.get("time"), "fields": paths[:MAX_FIELD_PATHS]})
    return out


# --------------------------------------------------------------------------- pod template revisions


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


def _env_pairs(container: dict) -> list[tuple[str, str]]:
    pairs = []
    for e in container.get("env") or []:
        if "value" in e:
            desc = json_short(str(e.get("value")), 80)
        else:
            vf = e.get("valueFrom") or {}
            ref = vf.get("secretKeyRef") or vf.get("configMapKeyRef") or vf.get("fieldRef") or {}
            source = next((k for k in ("secretKeyRef", "configMapKeyRef", "fieldRef") if vf.get(k)), "valueFrom")
            desc = f"from {source} {ref.get('name') or ref.get('fieldPath') or ''}{('.' + ref['key']) if ref.get('key') else ''}"
        pairs.append((e.get("name"), desc))
    return pairs


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
            if a.get(k) == b.get(k):
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
                changes.append(f"container {name} env {k} added: {map_b[k]}")
            elif k not in map_b:
                changes.append(f"container {name} env {k} removed")
            elif map_a[k] != map_b[k]:
                changes.append(f"container {name} env {k}: {map_a[k]} -> {map_b[k]}")
        names_a, names_b = [k for k, _ in env_a], [k for k, _ in env_b]
        for k in sorted({k for k in names_b if names_b.count(k) > 1} - {k for k in names_a if names_a.count(k) > 1}):
            values = [v for n, v in env_b if n == k]
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


def attach_template_changes(components: dict[str, dict], revisions: dict) -> None:
    """For workloads with revision history, the differences between the previous and the current pod template."""
    for comp in components.values():
        revs = revisions.get((comp["namespace"], comp["kind"], comp["name"])) or []
        if len(revs) < 2:
            continue
        previous, current = revs[-2], revs[-1]
        diff = template_diff(previous["template"], current["template"])
        if diff:
            comp["template_changes"] = {
                "revision": current["revision"],
                "changed_at": current["created"],
                "changes": diff,
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
) -> dict | None:
    """A change record for one object, or None when the object shows no separate recent write."""
    created = meta.get("creationTimestamp")
    modified = modified_after_creation(meta)
    late = created_after(created, targets_created or [])
    if modified is None and late is None:
        return None
    at = last_spec_write(meta) if modified is not None else created
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
    }
    if modified is not None:
        record["modified_after_creation_s"] = modified
        fields = [f for w in later_writes(meta) for f in w["fields"]]
        if fields:
            record["fields"] = list(dict.fromkeys(fields))[:MAX_FIELD_PATHS]
    if late is not None:
        record["created_after_targets_s"] = late
    if extra:
        record.update(extra)
    return record


# --------------------------------------------------------------------------- symptom onset and ranking


def estimate_symptom_onset(
    components: dict[str, dict], cluster: dict, collected_at: datetime
) -> tuple[datetime | None, str | None]:
    """The earliest start among abnormalities that are still active, outside telemetry components."""
    candidates: list[tuple[datetime, str]] = []
    for cid, comp in components.items():
        if comp.get("role") == "observability":
            continue
        for sig in comp.get("log_signals") or []:
            if sig.get("state") == "ongoing" and sig.get("first_seen_seconds_ago") is not None:
                candidates.append((collected_at - timedelta(seconds=sig["first_seen_seconds_ago"]), f"errors in {cid}"))
        for pod in comp.get("pods") or []:
            for key, what in (("not_ready_since", "pod not ready"), ("last_crash_at", "container crash")):
                moment = parse_time(pod.get(key))
                if moment and (key != "last_crash_at" or not pod.get("settled")):
                    candidates.append((moment, f"{what} in {cid}"))
            if pod.get("phase") == "Pending" and parse_time(pod.get("created")):
                candidates.append((parse_time(pod["created"]), f"pod pending in {cid}"))
        for ev in comp.get("warning_events") or []:
            moment = parse_time(ev.get("first_seen"))
            if moment:
                candidates.append((moment, f"{ev.get('reason')} events on {cid}"))
    for alert in cluster.get("firing_alerts") or []:
        moment = parse_time(alert.get("active_since"))
        if moment:
            candidates.append((moment, f"alert {alert.get('alertname')}"))
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
    }.get(kind, "it calls")


def relative_to_symptoms(before: int | None) -> str:
    if before is None:
        return ""
    if before >= 0:
        return f"; {before}s before the first active symptom"
    return f"; {-before}s after the first active symptom"


def describe_change(change: dict, component_id: str | None = None) -> str:
    """Signal text for a change, from the point of view of `component_id`."""
    ago = change.get("seconds_ago")
    rel = relative_to_symptoms(change.get("seconds_before_symptoms"))
    fields = f" (fields: {', '.join(change['fields'][:4])})" if change.get("fields") else ""
    template = ""
    if change.get("template_changes"):
        template = f"; pod template changes: {'; '.join(change['template_changes'][:3])}"
    if change["object"] == component_id:
        if change.get("modified_after_creation_s") is not None:
            return (
                f"spec modified {ago}s ago, {change['modified_after_creation_s']}s after this object was created"
                f"{fields}{template}{rel}"
            )
        return f"created {ago}s ago, {change['created_after_targets_s']}s after the workloads it calls{rel}"
    what = f"{change['kind']} {change['name']}"
    if change.get("modified_after_creation_s") is not None:
        return (
            f"{what} was modified {ago}s ago, {change['modified_after_creation_s']}s after it was created{fields}{rel}"
        )
    return f"{what} was created {ago}s ago, {change['created_after_targets_s']}s after the workloads {_acts_on(change['kind'])}{rel}"


def rank_changes(components: dict[str, dict], cluster: dict, collected_at: datetime | None = None) -> list[dict]:
    """Order recorded changes by their relation to the first active symptom and attach them as signals.

    Changes made before the first symptom come first, nearest first; then the rest, most recent first. Without
    an onset estimate the order is most recent first. Returns the full ranked list.
    """
    collected_at = collected_at or now()
    onset, source = estimate_symptom_onset(components, cluster, collected_at)
    cluster["symptom_onset"] = onset.isoformat(timespec="seconds") if onset else None
    cluster["symptom_onset_source"] = source
    ranked = []
    for raw in cluster.get("_changes") or []:
        change = dict(raw)
        at = parse_time(change.get("at"))
        change["seconds_ago"] = seconds_ago(at)
        change["seconds_before_symptoms"] = seconds_between(onset, at) if onset else None
        affected = [components[c] for c in change.get("affects") or [] if c in components]
        if affected and all(c.get("role") == "observability" for c in affected):
            change["role"] = "observability"
        ranked.append(change)

    def order(change: dict) -> tuple:
        before = change.get("seconds_before_symptoms")
        if before is None:
            return (1, change.get("seconds_ago") or 0)
        if before >= -ONSET_TOLERANCE_SECONDS:
            return (0, before)
        return (2, change.get("seconds_ago") or 0)

    ranked.sort(key=order)
    cluster["recent_changes"] = [
        {
            k: v
            for k, v in c.items()
            if k in ("object", "at", "seconds_ago", "seconds_before_symptoms", "fields", "role") or k.endswith("_s")
        }
        for c in ranked[:MAX_RECENT_CHANGES]
    ]
    flag = "nearest_change_before_symptoms" if onset else "most_recent_change"
    first = next((c for c in ranked if c.get("role") != "observability" and (not onset or order(c)[0] == 0)), None)
    for change in ranked:
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
