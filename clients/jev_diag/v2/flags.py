"""Feature flags as configuration: who a flag change acts on.

Applications that evaluate feature flags (OpenFeature with flagd, or any provider fed the same JSON schema) keep
their flag definitions in a ConfigMap: `{"flags": {"<key>": {"state": "ENABLED", "variants": {...},
"defaultVariant": "<name>"}}}`. Flipping a flag changes no workload: only that ConfigMap is written, and the
service that evaluates the flag starts misbehaving. The ConfigMap's owner (the flag server) shows the change but is
not where the fault acts.

So when a flag ConfigMap was written after the application was deployed, each flag that is on (its default variant
selects a value that is true, non-zero, or a non-empty string other than "off") is attributed to the workloads its
key names (by whole name tokens, as data-store naming is matched in deps.py) and becomes a change on them.
"""

from __future__ import annotations

import json
import re

from clients.jev_diag.v2.derive import GENERIC_NAME_SUFFIXES

# Words in flag keys that describe the switch, not the service it acts on.
_FLAG_WORDS = {
    "enable",
    "enabled",
    "disable",
    "disabled",
    "flag",
    "feature",
    "toggle",
    "failure",
    "fail",
    "error",
    "test",
}
_OFF = {"off", "false", "disabled", "none", "0", ""}
MAX_FLAGS = 3


def flag_definitions(configmap: dict) -> dict[str, dict]:
    """The flags a ConfigMap defines in the flagd JSON schema, keyed by flag key (empty when it defines none)."""
    out: dict[str, dict] = {}
    for value in (configmap.get("data") or {}).values():
        try:
            doc = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("flags"), dict):
            out.update({k: v for k, v in doc["flags"].items() if isinstance(v, dict)})
    return out


def is_on(flag: dict) -> bool:
    """Whether a flag is enabled and its default variant selects an `on` value."""
    if str(flag.get("state", "ENABLED")).upper() != "ENABLED":
        return False
    value = (flag.get("variants") or {}).get(flag.get("defaultVariant"))
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return isinstance(value, str) and value.strip().lower() not in _OFF


def key_tokens(key: str) -> set[str]:
    """Name tokens of a flag key (`paymentServiceFailure`, `ad-service-gc`), without switch words."""
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)
    tokens = {t.lower() for t in re.split(r"[\s_.\-]+", words) if t}
    return {t for t in tokens if len(t) >= 2} - _FLAG_WORDS - GENERIC_NAME_SUFFIXES


def flag_targets(key: str, components: dict[str, dict]) -> list[str]:
    """Workloads a flag key names: every non-generic token of some workload's name appears in the key."""
    tokens = key_tokens(key)
    out = []
    for cid, comp in components.items():
        name = {t for t in re.split(r"[-_.]", comp["name"].lower()) if t} - GENERIC_NAME_SUFFIXES
        if name and name <= tokens:
            out.append(cid)
    return sorted(out)


def attribute_flags(components: dict[str, dict], config_objects: dict, changed: set[str]) -> None:
    """For each flag ConfigMap in `changed` (object ids written after deploy), the flags that are on become a change
    on the workloads they name."""
    for (kind, ns, name), entry in config_objects.items():
        if kind != "ConfigMap" or f"configmap/{ns}/{name}" not in changed:
            continue
        flags = flag_definitions(entry.get("_object") or {})
        on = [k for k, f in flags.items() if is_on(f)][: MAX_FLAGS * 4]
        for key in on:
            for cid in flag_targets(key, components)[:MAX_FLAGS]:
                text = (
                    f"feature flag {key} is on (ConfigMap {name} was written after deploy; its default variant "
                    f"{flags[key].get('defaultVariant')!r} selects {json.dumps((flags[key].get('variants') or {}).get(flags[key].get('defaultVariant')))}); "
                    "the flag names this service, which evaluates it"
                )
                if text not in components[cid]["signals"]:
                    components[cid]["signals"].append(text)
