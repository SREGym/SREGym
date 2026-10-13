"""Readable one-line form of structured (JSON) log lines.

Many services log one JSON object per line (MongoDB, and Go, Java or Python services using structured logging).
Their first two hundred characters are timestamps, ids and context fields; the words that say what failed sit deep
inside nested fields (MongoDB's `attr.message`), so the line is cut before them wherever it is shortened. Here such
a line is rewritten as `<level> <component> <msg>: key=value; ...` with the bookkeeping fields dropped, so what it
says comes first.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Bookkeeping fields: when, where in the code, which thread or request.
DROPPED_KEYS = {
    "t",
    "ts",
    "time",
    "timestamp",
    "@timestamp",
    "$date",
    "date",
    "id",
    "ctx",
    "caller",
    "thread",
    "thread_name",
    "pid",
    "tid",
    "hostname",
    "host",
    "logger_name",
    "seq",
}
LEVEL_KEYS = ("level", "severity", "lvl", "s", "loglevel")
MESSAGE_KEYS = ("msg", "message", "event")
COMPONENT_KEYS = ("c", "component", "logger", "module")
LEVEL_LETTERS = {"F": "FATAL", "E": "ERROR", "W": "WARN", "I": "INFO", "D": "DEBUG"}
# Containers that only group fields ("attr": {...}): their name adds nothing to the field names.
GROUP_KEYS = {"attr", "attrs", "fields", "data", "context", "extra", "details"}
# Fields that carry the explanation; they go first, so a shortened line keeps them.
TELLING_RE = re.compile(r"(?i)(message|msg|error|err|reason|cause|exception|detail)$")
# Leading "[1791678316:863177][1:0xffff91c6345]"-style stamps inside a message.
_STAMPS = re.compile(r"^(\s*\[[0-9a-fx:. ]*\])+\s*", re.IGNORECASE)


def _flat(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    """(dotted key, text) pairs of a nested value, bookkeeping keys left out."""
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for k, v in value.items():
            if k in DROPPED_KEYS:
                continue
            out += _flat(v, "" if k in GROUP_KEYS and not prefix else (f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(value, list):
        return [(prefix, json.dumps(value, ensure_ascii=False, default=str)[:200])]
    text = _STAMPS.sub("", str(value)).lstrip(" ,:") if isinstance(value, str) else str(value)
    return [(prefix, text)]


def compact(line: str) -> str:
    """The line with an embedded JSON log object rewritten as `<level> <component> <msg>: k=v; ...`; any other line
    unchanged. Text before the object (a kubectl `[pod/...]` prefix and timestamp) is kept."""
    start = line.find("{")
    if start < 0 or not line.rstrip().endswith("}"):
        return line
    try:
        obj = json.loads(line[start:])
    except ValueError:
        return line
    if not isinstance(obj, dict):
        return line

    def take(keys: tuple[str, ...]) -> str:
        """The first non-empty scalar among `keys`, removed from the record; '' when there is none."""
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (str, int, float)) and str(v):
                obj.pop(k)
                return str(v)
        return ""

    level = take(LEVEL_KEYS)
    level = LEVEL_LETTERS.get(level.upper(), level.upper()) if len(level) <= 1 else level.upper()
    head = " ".join(x for x in (level, take(COMPONENT_KEYS), _STAMPS.sub("", take(MESSAGE_KEYS))) if x)
    pairs = [(k, v) for k, v in _flat(obj) if v not in ("", "None", "null")]
    pairs.sort(key=lambda kv: (not TELLING_RE.search(kv[0]) or len(kv[1]) < 8, 0))
    fields = "; ".join(f"{k}={v}" for k, v in pairs)
    return line[:start] + head + (f": {fields}" if fields else "")
