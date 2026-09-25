"""Two light samples an agent-chosen interval apart, read only as trends.

The first sample can be taken at any point before the full collection, and nothing here assumes it was taken
before or after a fault. Only growth between the samples is reported: restart counts that rose, pods that stayed
Pending, and warning events that kept repeating. The second sample is built from the pods and events the full
collection reads anyway, so it costs no extra kubectl calls.
"""

from __future__ import annotations

from clients.jev_diag.timeutil import now, parse_time, seconds_between


def sample_from_objects(pods: dict[str, list[dict]], events: dict[str, list[dict]]) -> dict:
    """A trend sample from raw pod and event objects keyed by namespace."""
    out: dict = {"at": now().isoformat(), "pods": {}, "events": {}}
    for ns, items in pods.items():
        for pod in items:
            meta, st = pod.get("metadata") or {}, pod.get("status") or {}
            statuses = (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or [])
            out["pods"][f"{ns}/{meta.get('name')}"] = {
                "phase": st.get("phase"),
                "restarts": {cs.get("name"): int(cs.get("restartCount") or 0) for cs in statuses},
            }
    for ns, items in events.items():
        for ev in items:
            if ev.get("type") != "Warning":
                continue
            obj = ev.get("involvedObject") or {}
            key = f"{ns}/{(obj.get('kind') or '?').lower()}/{obj.get('name')}/{ev.get('reason')}"
            count = int(ev.get("count") or (ev.get("series") or {}).get("count") or 1)
            out["events"][key] = max(count, out["events"].get(key, 0))
    return out


def collect_sample(namespaces: list[str]) -> dict:
    """The first sample: one pod list and one event list per namespace."""
    from clients.jev_diag.collector import kubectl_items

    errors: list[str] = []
    pods = {ns: kubectl_items("pods", ns, errors, managed_fields=False) for ns in namespaces}
    events = {ns: kubectl_items("events", ns, errors, managed_fields=False) for ns in namespaces}
    sample = sample_from_objects(pods, events)
    if errors:
        sample["errors"] = errors[:5]
    return sample


def apply_trend(components: dict[str, dict], before: dict, after: dict) -> dict:
    """Attach growth observed between two samples to the components that own the pods and event targets."""
    interval = seconds_between(parse_time(after.get("at")), parse_time(before.get("at"))) or 0
    pod_owner = {
        f"{comp['namespace']}/{p.get('name')}": cid for cid, comp in components.items() for p in comp.get("pods") or []
    }
    observations = 0
    for key, cur in (after.get("pods") or {}).items():
        cid = pod_owner.get(key)
        old = (before.get("pods") or {}).get(key)
        if cid is None or old is None:
            continue
        pod = key.split("/", 1)[1]
        for container, count in cur["restarts"].items():
            grew = count - old["restarts"].get(container, count)
            if grew > 0:
                components[cid]["signals"].append(
                    f"pod {pod}: container {container} restarted {grew} more time(s) in the last {interval}s"
                )
                observations += 1
        if cur.get("phase") == "Pending" and old.get("phase") == "Pending":
            components[cid]["signals"].append(f"pod {pod} is still in pod phase Pending after another {interval}s")
            observations += 1
    for key, count in (after.get("events") or {}).items():
        grew = count - (before.get("events") or {}).get(key, count)
        if grew <= 0:
            continue
        ns, kind, name, reason = key.split("/", 3)
        cid = pod_owner.get(f"{ns}/{name}") if kind == "pod" else None
        if cid is None:
            continue
        components[cid]["signals"].append(
            f"warning event {reason} on {kind}/{name} repeated {grew} more time(s) in the last {interval}s"
        )
        observations += 1
    return {"interval_s": interval, "observations": observations}
