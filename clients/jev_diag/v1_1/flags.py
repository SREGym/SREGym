"""Feature flags as configuration: what a flag write changed, and which failing workloads use the flags.

Applications that evaluate feature flags (OpenFeature with flagd, or any provider fed the same JSON schema) keep
their flag definitions in a ConfigMap: `{"flags": {"<key>": {"state": "ENABLED", "variants": {...},
"defaultVariant": "<name>"}}}`. Flipping a flag writes only that ConfigMap. The workload that reads it (the flag
provider) shows the write, but the fault acts in the clients that evaluate the flag.

When a flag ConfigMap was written after the application was deployed, the flags whose state, default variant or
selected value changed are listed old -> new, from the last applied configuration. Without it the previous content is
unknown, so only the flags that are on now are listed, and said to be on, not changed. The list goes, as a signal, on
the provider's clients (workloads configured to call it) that are failing. Which client evaluates which flag is not
visible in the cluster, and the signal says so: flags are never attributed by their key's wording.
"""

from __future__ import annotations

import json

LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
MAX_FLAGS = 5
_OFF = {"off", "false", "disabled", "none", "0", ""}


def flag_definitions(configmap: dict) -> dict[str, dict]:
    """The flags a ConfigMap (or its `data`) defines in the flagd JSON schema, by key; empty when it defines none."""
    out: dict[str, dict] = {}
    for value in (configmap.get("data") or {}).values():
        try:
            doc = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("flags"), dict):
            out.update({k: v for k, v in doc["flags"].items() if isinstance(v, dict)})
    return out


def previous_flags(configmap: dict) -> dict[str, dict] | None:
    """The flags of the last applied configuration, or None when the ConfigMap keeps none."""
    try:
        applied = json.loads(((configmap.get("metadata") or {}).get("annotations") or {}).get(LAST_APPLIED) or "null")
    except ValueError:
        return None
    return flag_definitions(applied) if isinstance(applied, dict) else None


def selected(flag: dict) -> object:
    return (flag.get("variants") or {}).get(flag.get("defaultVariant"))


def is_on(flag: dict) -> bool:
    """Whether a flag is enabled and its default variant selects an `on` value."""
    if str(flag.get("state", "ENABLED")).upper() != "ENABLED":
        return False
    value = selected(flag)
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return isinstance(value, str) and value.strip().lower() not in _OFF


def describe_flag(flag: dict) -> str:
    return (
        f"{flag.get('state', 'ENABLED')}, defaultVariant {flag.get('defaultVariant')!r} = {json.dumps(selected(flag))}"
    )


def flag_changes(old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    """Flags whose state, default variant or selected value differ, old -> new."""
    out = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before is None:
            out.append(f"flag {key} added ({describe_flag(after)})")
        elif after is None:
            out.append(f"flag {key} removed")
        elif describe_flag(before) != describe_flag(after):
            out.append(f"flag {key}: {describe_flag(before)} -> {describe_flag(after)}")
    return out[:MAX_FLAGS]


def _failing(comp: dict) -> bool:
    replicas = comp.get("replicas") or {}
    short = (replicas.get("ready") or 0) < (replicas.get("desired") or 0)
    return bool(comp.get("log_findings") or comp.get("warning_events") or short)


def attribute_flags(components: dict[str, dict], config_objects: dict, changed: set[str]) -> None:
    """For each flag ConfigMap in `changed` (object ids written after deploy), what it changed goes on the failing
    clients of the workloads that read it."""
    for (kind, ns, name), entry in config_objects.items():
        if kind != "ConfigMap" or f"configmap/{ns}/{name}" not in changed:
            continue
        obj = entry.get("_object") or {}
        now = flag_definitions(obj)
        if not now:
            continue
        before = previous_flags(obj)
        if before is not None:
            lines, what = flag_changes(before, now), "changed"
        else:
            lines, what = [f"flag {k} ({describe_flag(f)})" for k, f in now.items() if is_on(f)][:MAX_FLAGS], "on now"
        if not lines:
            continue
        providers = {
            c
            for c, comp in components.items()
            if comp["namespace"] == ns and f"configmap/{name}" in (comp.get("config_objects") or {})
        }
        for cid, comp in components.items():
            served_by = sorted(providers & set(comp.get("_calls") or []))
            if cid in providers or not served_by or not _failing(comp):
                continue
            comp["signals"].append(
                f"uses feature flags served by {', '.join(served_by)}, whose ConfigMap {name} was written after "
                f"deploy; flags {what}: {'; '.join(lines)} (which of them this workload evaluates is not visible "
                "in the cluster)"
            )
