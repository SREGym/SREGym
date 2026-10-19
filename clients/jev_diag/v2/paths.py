"""Call paths whose calls time out while the callee shows no failure of its own.

The facts are both sides' call settings: on the caller, timeouts, deadlines, retries and backoff for that callee
(env names that carry the callee's name, or keys of the caller's own ConfigMaps); on the callee, the settings that
cap how much work it takes (rate limits, queues, workers, concurrency). Settings are read by name pattern only;
no value is judged here.

A path is a feedback loop when the caller retries timed-out calls and the callee caps its throughput: each slow
period multiplies the requests the callee receives, which keeps it at its cap. Only then is the path itself reported
as the cause (report.subject); otherwise its facts are details of the component.
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
MAX_VALUE_CHARS = 40
MAX_SETTINGS = 8
# key: value / key=value / "key": value inside a ConfigMap file
_INLINE = re.compile(r"""(?P<key>[A-Za-z_][\w.-]{2,60})["']?\s*[:=]\s*["']?(?P<value>[\w.+-]{1,40})""")
_POINTER_PREFIXES = ("errors logged by", "server ")


def setting_pairs(data: dict[str, str]) -> dict[str, str]:
    """Call and capacity settings in a ConfigMap's data: short keys with short values, and key/value pairs inside
    file-like values."""
    out: dict[str, str] = {}
    for key, value in (data or {}).items():
        text = str(value)
        if "\n" not in text and len(text) <= MAX_VALUE_CHARS:
            if CALL_SETTING.search(key) or CAPACITY_SETTING.search(key):
                out[key] = text
            continue
        for m in _INLINE.finditer(text):
            if CALL_SETTING.search(m.group("key")) or CAPACITY_SETTING.search(m.group("key")):
                out.setdefault(f"{key}:{m.group('key')}", m.group("value"))
    return out


def _token(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _towards(settings: dict[str, str], callee: str, others: list[str]) -> list[str]:
    """Caller settings for calls to `callee`: env names carrying the callee's name, and ConfigMap keys that name no
    other component (the caller's own call configuration)."""
    mine, out = _token(callee), []
    for name, value in settings.items():
        if not CALL_SETTING.search(name.split(":")[-1]):
            continue
        flat = _token(name)
        named = bool(mine) and mine in flat
        own_config = ":" in name and not any(_token(o) and _token(o) in flat for o in others)
        if named or own_config:
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
            caller_settings = _towards(caller.get("_call_settings") or {}, names[callee_id], others)
            callee_settings = [
                f"{k}={v}"
                for k, v in (callee.get("_call_settings") or {}).items()
                if CAPACITY_SETTING.search(k.split(":")[-1])
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
        text += (
            f"; {caller} retries calls that time out, so every slow period multiplies the requests {callee} receives "
            "while it is capped"
        )
    return text
