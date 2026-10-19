"""Call paths whose calls time out while the callee shows no failure of its own.

The facts are both sides' call settings: on the caller, timeouts, deadlines, retries and backoff for that callee
(env names that carry the callee's name as whole words, or keys of the caller's own ConfigMaps); on the callee,
the settings that cap how much work it takes (rate limits, queues, workers, concurrency). A setting is read by its
name and needs a numeric value; telemetry and tracing settings are left out. No value is judged here.

A path is marked a feedback loop when the caller has a retry and a timeout setting for the callee and the callee has
a capacity setting. That is read from the settings alone; no request rate or latency is measured, so the text
states only the settings.
"""

from __future__ import annotations

import re

CALL_SETTING = re.compile(r"(?i)timeout|deadline|retr(?:y|ies)|attempts?|backoff")
RETRY_SETTING = re.compile(r"(?i)retr(?:y|ies)|attempts?")
WAIT_SETTING = re.compile(r"(?i)timeout|deadline")
CAPACITY_SETTING = re.compile(
    r"(?i)qps|rps|rate_?limit|throttl|queue|concurren|workers?(?:$|_)|capacity|max_?(?:inflight|in_flight|requests|conn)"
)
TIMEOUT_ERROR = re.compile(r"(?i)deadline.?exceeded|timed?.?out|timeout")
NUMERIC_VALUE = re.compile(r"(?i)^\d+(?:\.\d+)?\s*(?:ns|us|ms|s|m|h)?$")  # a count, a rate, or a duration
MAX_VALUE_CHARS = 40
MAX_SETTINGS = 8
# key: value / key=value / "key": value inside a ConfigMap file
_INLINE = re.compile(r"""(?P<key>[A-Za-z_][\w.-]{2,60})["']?\s*[:=]\s*["']?(?P<value>[\w.+-]{1,40})""")
_POINTER_PREFIXES = ("errors logged by", "server ")


def setting_pairs(data: dict[str, str]) -> dict[str, str]:
    """Call and capacity settings in a ConfigMap's data: short keys with short values, and key/value pairs inside
    file-like values whose key has one value in the file."""
    out: dict[str, str] = {}
    for key, value in (data or {}).items():
        text = str(value)
        if "\n" not in text and len(text) <= MAX_VALUE_CHARS:
            if _is_setting(key, text):
                out[key] = text
            continue
        found: dict[str, set[str]] = {}
        for m in _INLINE.finditer(text):
            if _is_setting(m.group("key"), m.group("value")):
                found.setdefault(m.group("key"), set()).add(m.group("value"))
        # a key repeated with different values (one block per service in a shared file) is ambiguous: left out
        out.update({f"{key}:{k}": next(iter(values)) for k, values in found.items() if len(values) == 1})
    return out


def _is_setting(name: str, value: str) -> bool:
    return bool(NUMERIC_VALUE.match(value.strip())) and bool(CALL_SETTING.search(name) or CAPACITY_SETTING.search(name))


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]


def _carries_name(setting: str, component: str) -> bool:
    """Whether a setting's name carries the component's name as whole words (`ad` is not in READ_TIMEOUT)."""
    want, have = _words(component), _words(setting)
    return bool(want) and any(have[i : i + len(want)] == want for i in range(len(have) - len(want) + 1))


def _own_settings(comp: dict) -> dict[str, str]:
    """The component's call and capacity settings, without telemetry and tracing settings."""
    from clients.jev_diag.v2.derive import TELEMETRY_RE  # derive imports this module

    return {k: v for k, v in (comp.get("_call_settings") or {}).items() if not TELEMETRY_RE.search(k)}


def _towards(settings: dict[str, str], callee: str, others: list[str]) -> list[str]:
    """Caller settings for calls to `callee`: env names carrying the callee's name, and ConfigMap keys that name no
    other component (the caller's own call configuration)."""
    out = []
    for name, value in settings.items():
        if not CALL_SETTING.search(name.split(":")[-1]):
            continue
        own_config = ":" in name and not any(_carries_name(name, o) for o in others)
        if _carries_name(name, callee) or own_config:
            out.append(f"{name}={value}")
    return out[:MAX_SETTINGS]


def own_failure(comp: dict) -> bool:
    """Whether the component shows a failure of its own (state, own error lines), not counting others' errors."""
    if any(not s.startswith(_POINTER_PREFIXES) for s in comp.get("signals") or []):
        return True
    return any(f["classes"] and "telemetry export failure" not in f["classes"] for f in comp.get("log_findings") or [])


def call_paths(components: dict[str, dict]) -> list[dict]:
    """Timing-out call paths with both sides' settings; each path's facts are also put on both components."""
    for comp in components.values():
        comp.pop("call_paths", None)
    names = {cid: comp.get("name") or cid for cid, comp in components.items()}
    out = []
    for caller_id, caller in components.items():
        counts: dict[str, int] = {}
        examples: dict[str, str] = {}
        for f in caller.get("log_findings") or []:
            if not TIMEOUT_ERROR.search(f.get("line") or ""):
                continue
            for target in f.get("targets") or []:
                counts[target] = counts.get(target, 0) + int(f.get("count") or 1)
                examples.setdefault(target, f["line"])
        for callee_id, n in counts.items():
            callee = components.get(callee_id)
            if callee is None or own_failure(callee):
                continue
            others = [names[c] for c in components if c not in (caller_id, callee_id)]
            caller_settings = _towards(_own_settings(caller), names[callee_id], others)
            callee_settings = [
                f"{k}={v}" for k, v in _own_settings(callee).items() if CAPACITY_SETTING.search(k.split(":")[-1])
            ][:MAX_SETTINGS]
            if not caller_settings:
                continue
            loop = (
                any(RETRY_SETTING.search(s.split("=")[0]) for s in caller_settings)
                and any(WAIT_SETTING.search(s.split("=")[0]) for s in caller_settings)
                and bool(callee_settings)
            )
            path = {
                "caller": caller_id,
                "callee": callee_id,
                "timeout_lines": n,
                "example": examples[callee_id][:160],
                "caller_settings": caller_settings,
                "callee_settings": callee_settings,
                "feedback_loop": loop,
            }
            out.append(path)
            fact = describe(path, names)
            caller.setdefault("call_paths", []).append(fact)
            callee.setdefault("call_paths", []).append(fact)
    return out


def describe(path: dict, names: dict[str, str]) -> str:
    caller, callee = names.get(path["caller"], path["caller"]), names.get(path["callee"], path["callee"])
    text = (
        f"calls from {caller} to {callee} time out ({path['timeout_lines']} lines, still occurring) while {callee} "
        f"shows no failure of its own; {caller}'s call settings for it: {', '.join(path['caller_settings'])}"
    )
    if path["callee_settings"]:
        text += f"; {callee}'s capacity settings: {', '.join(path['callee_settings'])}"
    if path["feedback_loop"]:
        retry = next(s for s in path["caller_settings"] if RETRY_SETTING.search(s.split("=")[0]))
        text += (
            f"; {caller} retries a timed-out call ({retry}), adding requests to {callee}, which caps its throughput "
            f"({path['callee_settings'][0]})"
        )
    return text
