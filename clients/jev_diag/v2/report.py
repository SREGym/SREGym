"""The submitted diagnosis, built by code from the investigation's choices and the collected facts. No model
writes text.

Five lines, in this order: Root cause, Mechanism, Details, Effect, Affected. The subject of the root-cause sentence
is the object that holds the bad value or the bad state, and the other object involved is named in the same
sentence with a relation word ("acting on", "its ConfigMap"). Changed values are written old -> new. Callers with
symptoms are listed as affected, never as causes. There is no fault-category line and no probabilities.
"""

from __future__ import annotations

import re

from clients.jev_diag.timeutil import parse_time, seconds_between
from clients.jev_diag.v2 import paths
from clients.jev_diag.v2.classifier import CARRIER_KINDS, Diagnosis
from clients.jev_diag.v2.collector import ClusterSnapshot
from clients.jev_diag.v2.derive import TELEMETRY_RE

MAX_DETAILS = 4
MAX_AFFECTED = 3
QUOTE_CHARS = 160
CONFIG_KINDS = ("ConfigMap", "Secret")
SAME_CHANGE_SECONDS = 10  # one change applied to several workloads lands within this window
# Signals that state the component's own condition (used for the Effect line).
STATE_WORDS = re.compile(
    r"(?i)desired replicas are ready|CrashLoopBackOff|OOMKilled|ready endpoints|Pending|Unschedulable|"
    r"ImagePullBackOff|ErrImagePull|not ready|restarted|terminat|evicted|at its limit|failed to start"
)
_CONTAINER = re.compile(r"\bcontainer \S+ ")
_TIMESTAMP = re.compile(r"^\[?\S*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s\]]*\]?\s*")
_JSON_MSG = re.compile(r'"msg"\s*:\s*"(?P<msg>[^"]+)"')
_JSON_ERR = re.compile(r'"(?:error|errmsg|reason)"\s*:\s*"(?P<err>[^"]+)"')


def describe(kind: str, name: str, namespace: str | None) -> str:
    return f"{kind} `{name}`" + (f" in namespace `{namespace}`" if namespace else "")


def component_phrase(comp: dict, *, both_kinds: bool) -> str:
    """'Deployment `x` in namespace `ns`', or with a Service of the same name: 'Service `x` (Deployment `x`) in ...'."""
    same_service = any(s.get("name") == comp["name"] for s in comp.get("services") or [])
    if both_kinds and same_service:
        return f"Service `{comp['name']}` ({comp['kind']} `{comp['name']}`) in namespace `{comp['namespace']}`"
    return describe(comp["kind"], comp["name"], comp["namespace"])


def quote(line: str) -> str:
    """One log line as quotable text: the message of a structured line, without timestamps or the container prefix."""
    body = line.split(": ", 1)[1] if ": " in line[:60] and not line.lstrip().startswith("{") else line
    msg = _JSON_MSG.search(body)
    if msg:
        err = _JSON_ERR.search(body)
        body = msg.group("msg") + (f": {err.group('err')}" if err else "")
    body = _TIMESTAMP.sub("", body.strip())
    return body[:QUOTE_CHARS].rstrip(" .,;") + ("..." if len(body) > QUOTE_CHARS else "")


def own_error_line(comp: dict) -> str | None:
    """The component's own still-occurring error line, else the last line its crashed container printed."""
    for sig in comp.get("log_signals") or []:
        if sig.get("state") != "stopped" and sig.get("line") and not TELEMETRY_RE.search(sig["line"]):
            return quote(sig["line"])
    for lines in (comp.get("previous_container_logs") or {}).values():
        for line in reversed(lines or []):
            if line.strip():
                return quote(line)
    return None


def key_fact(d: Diagnosis) -> str | None:
    if d.evidence_result is None:
        return None
    return next((e["text"] for e in d.evidence if e["id"] == d.evidence_result.choice), None)


def changed_values(comp: dict) -> list[str]:
    """The workload's pod template differences (old -> new), restart annotations left out."""
    tc = comp.get("template_changes") or {}
    if tc.get("restart_only"):
        return []
    return [c for c in tc.get("changes") or [] if "pod-template-hash" not in c and "restartedAt" not in c]


def same_change(snapshot: ClusterSnapshot, cid: str) -> list[str]:
    """Other workloads whose pod template changed the same way (names aside) within the same few seconds: one
    change applied to several workloads, reported as one cause."""
    comp = snapshot.components.get(cid) or {}
    mine = _normalized(comp)
    if not mine:
        return []
    at = parse_time((comp.get("template_changes") or {}).get("changed_at"))
    out = []
    for other_id, other in snapshot.components.items():
        if other_id == cid or _normalized(other) != mine:
            continue
        other_at = parse_time((other.get("template_changes") or {}).get("changed_at"))
        gap = seconds_between(other_at, at) if other_at and at else None
        if gap is not None and abs(gap) <= SAME_CHANGE_SECONDS:
            out.append(other_id)
    return sorted(out)


def _normalized(comp: dict) -> tuple[str, ...]:
    """A workload's template changes with its own name and its container names taken out."""
    name = comp.get("name") or ""
    return tuple(sorted(_CONTAINER.sub("container <c> ", c).replace(name, "<name>") for c in changed_values(comp)))


def users_of(snapshot: ClusterSnapshot, fo: dict) -> list[str]:
    return [c for c in fo.get("components") or [] if c in snapshot.components]


def feedback_path(snapshot: ClusterSnapshot, d: Diagnosis) -> dict | None:
    """The retry/capacity loop the concluded component is one end of, when the fault object is not a config
    object (a ConfigMap holding the settings is the subject itself)."""
    fo = d.fault_object
    if fo and fo["kind"] in CONFIG_KINDS:
        return None
    for path in snapshot.cluster.get("call_paths") or []:
        if path.get("feedback_loop") and d.component in (path["caller"], path["callee"]):
            return path
    return None


def path_phrase(snapshot: ClusterSnapshot, path: dict) -> str:
    caller, callee = snapshot.components[path["caller"]], snapshot.components[path["callee"]]
    where = f" in namespace `{callee['namespace']}`" if caller["namespace"] == callee["namespace"] else ""
    return (
        f"the call path from {describe(caller['kind'], caller['name'], None if where else caller['namespace'])} to "
        f"{describe(callee['kind'], callee['name'], None if where else callee['namespace'])}{where}"
    )


def subject(snapshot: ClusterSnapshot, d: Diagnosis) -> tuple[str, str | None]:
    """(subject phrase, object named alongside it) by where the bad value lives."""
    comp = snapshot.components.get(d.component)
    fo = d.fault_object
    app_namespaces = set(snapshot.app.get("namespaces") or [])
    if comp is not None and d.blocked_input:
        blocked = d.blocked_input
        source = blocked.get("topic") or blocked.get("input")
        if source:
            return f"the input `{source}` consumed by {describe(comp['kind'], comp['name'], comp['namespace'])}", None
    path = None if d.blocked_input else feedback_path(snapshot, d)
    if path:
        return path_phrase(snapshot, path), None
    if fo:
        fo_phrase = describe(fo["kind"], fo["name"], fo.get("namespace"))
        outside = not fo.get("namespace") or fo.get("namespace") not in app_namespaces
        if comp is None:
            return fo_phrase, None
        acted = describe(comp["kind"], comp["name"], comp["namespace"])
        if fo["kind"] in CARRIER_KINDS or fo["kind"] == "NetworkPolicy" or outside:
            return f"{fo_phrase}, acting on {acted}", None
        if fo["kind"] == "Service":
            return f"{fo_phrase} (selecting {acted})", None
        if fo["kind"] in CONFIG_KINDS:
            users = users_of(snapshot, fo)
            if len(users) > 1:
                return f"{fo_phrase}, used by {', '.join(users)}", None
            return component_phrase(comp, both_kinds=True), f"through its {fo['kind']} `{fo['name']}`"
    if comp is None:
        return f"`{d.component}`", None
    has_change = "change" in (comp.get("evidence_kinds") or []) or bool(changed_values(comp))
    group = same_change(snapshot, d.component) if has_change else []
    if group:
        names = ", ".join(f"`{snapshot.components[c]['name']}`" for c in [d.component, *group])
        return f"{comp['kind']}s {names} in namespace `{comp['namespace']}` (the same change on {len(group) + 1})", None
    return component_phrase(comp, both_kinds=not has_change), None


def build_report(snapshot: ClusterSnapshot, d: Diagnosis) -> str:
    text = cause_block(snapshot, d)
    if d.second is not None:
        text += "\n\nA second, independent root cause:\n" + cause_block(snapshot, d.second)
    return text


def cause_block(snapshot: ClusterSnapshot, d: Diagnosis) -> str:
    comp = snapshot.components.get(d.component) or {}
    fo = d.fault_object
    subj, carrier = subject(snapshot, d)
    fact = key_fact(d)
    changes = changed_values(comp)
    path = None if d.blocked_input else feedback_path(snapshot, d)
    names = {cid: c.get("name") or cid for cid, c in snapshot.components.items()}
    what = (paths.describe(path, names) if path else None) or fact or (fo or {}).get("signal")
    what = what or (changes[0] if changes else None)
    if what is None:
        what = next((s for s in comp.get("signals") or [] if not STATE_WORDS.search(s)), None)
    first = f"Root cause: {subj}" + (f", {carrier}" if carrier else "")
    lines = [first + (f": {what.rstrip('.')}." if what else ".")]

    error = own_error_line(comp) if comp else None
    mechanism = None
    if d.blocked_input and comp:
        b = d.blocked_input
        mechanism = (
            f"one input item fails processing again and again and blocks the items behind it: {b.get('evidence')}"
            if b.get("source") != "broker"
            else f"consumer group {b.get('group')} is stalled at offset {b.get('offset')} of topic {b.get('topic')}"
        )
    elif fo and fo.get("signal") and fo["signal"] != what:
        mechanism = fo["signal"]
    parts = [m.rstrip(".") for m in (mechanism,) if m]
    if path:
        parts.append(f'{names[path["caller"]]} logs "{quote(path["example"])}"')
        error = None if d.component == path["caller"] else error
    if error and (not what or error not in what):
        parts.append(f'the component logs "{error}"')
    if not parts and what:
        parts.append(what.rstrip("."))
    if parts:
        lines.append("Mechanism: " + "; ".join(parts) + ".")

    details = [c for c in changes if c != what]
    for step in (d.investigation or {}).get("steps") or []:
        if step.get("component") == d.component:
            details += [x for x in step.get("spec_checks") or [] if x != what]
    details = list(dict.fromkeys(x.rstrip(".") for x in details if x))[:MAX_DETAILS]
    if details:
        lines.append("Details: " + "; ".join(details) + ".")

    if comp:
        state = [s for s in comp.get("signals") or [] if STATE_WORDS.search(s)][:2]
        effect = "; ".join(s.rstrip(".") for s in state) if state else "its pods are Running and Ready"
        classes: dict[str, int] = {}
        for by_class in (comp.get("referenced_by_errors_from") or {}).values():
            for klass, n in by_class.items():
                classes[klass] = classes.get(klass, 0) + n
        if classes:
            seen = ", ".join(f"{k} ({n} lines)" for k, n in sorted(classes.items(), key=lambda kv: -kv[1])[:3])
            effect += f"; its callers see {seen}"
        lines.append(f"Effect: {effect}.")

    affected: list[str] = []
    for src, by_class in sorted(
        (comp.get("referenced_by_errors_from") or {}).items(), key=lambda kv: -sum(kv[1].values())
    )[:MAX_AFFECTED]:
        other = snapshot.components.get(src) or {}
        name = describe(other.get("kind", "component"), other.get("name", src), other.get("namespace"))
        klass = ", ".join(sorted(by_class))
        affected.append(f"{name}: {klass} calling it ({sum(by_class.values())} lines, still occurring)")
    if fo and not affected:
        others = [c for c in users_of(snapshot, fo) if c != d.component][:MAX_AFFECTED]
        affected += [describe(snapshot.components[c]["kind"], snapshot.components[c]["name"], None) for c in others]
    if affected:
        lines.append("Affected (symptoms only, not causes): " + "; ".join(affected) + ".")
    return "\n".join(lines)
