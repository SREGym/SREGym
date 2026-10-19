"""The submitted diagnosis, built by code from the investigation's choices and the collected facts. No model
writes text.

Lines, in this order: Root cause, Mechanism, Details, Effect, Observed, Affected. The subject of the root-cause
sentence is the object that holds the bad value or the bad state, and the other object involved is named in the same
sentence with its relation. Which object that is depends on the kind of relation (RELATIONS: a blocked input, a
retry call path, a job driving a failing component, an object carrying the bad value, one change on several
workloads, or the workload itself). Changed values are written old -> new. Callers with symptoms are listed as
affected, never as causes. There is no fault-category line and no probabilities. A second, independent cause gets its
own block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from clients.jev_diag.timeutil import parse_time, seconds_between
from clients.jev_diag.v2 import checks, paths
from clients.jev_diag.v2.classifier import CARRIER_KINDS, Diagnosis
from clients.jev_diag.v2.collector import ClusterSnapshot, is_error_line
from clients.jev_diag.v2.derive import DEPENDENCY_CLASSES, TELEMETRY_RE

MAX_DETAILS = 4
MAX_AFFECTED = 3
MAX_OBSERVED = 5
QUOTE_CHARS = 160
CONFIG_KINDS = ("ConfigMap", "Secret")
SAME_CHANGE_SECONDS = 10  # one change applied to several workloads lands within this window
# Signals that state the component's own condition (used for the Effect line).
STATE_WORDS = re.compile(
    r"(?i)desired replicas are ready|CrashLoopBackOff|OOMKilled|ready endpoints|Pending|Unschedulable|"
    r"ImagePullBackOff|ErrImagePull|not ready|restarted\b|terminat|evicted|at its limit|failed to start|"
    r"complet|still active|pile up"
)
_CAUSAL = re.compile(r"(?i)\bso\b|because|which (?:is|are|blocks|prevents)")
_POINTERS = ("errors logged by", "server ")
_CONTAINER = re.compile(r"\bcontainer \S+ ")
_NAME_PREFIX = re.compile(r"^(?:\[pod/[^\]]+\]\s?)?[a-z0-9][a-z0-9./-]{0,58}: ")
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
    prefix = _NAME_PREFIX.match(line)  # "container-name: " as the collector writes it, never "FATAL: " etc.
    body = line[prefix.end() :] if prefix else line
    msg = _JSON_MSG.search(body)
    if msg:
        err = _JSON_ERR.search(body)
        body = msg.group("msg") + (f": {err.group('err')}" if err else "")
    body = _TIMESTAMP.sub("", body.strip())
    return body[:QUOTE_CHARS].rstrip(" .,;") + ("..." if len(body) > QUOTE_CHARS else "")


def own_error_line(comp: dict) -> str | None:
    """The component's own still-occurring error line, else the last error line its crashed container printed (a
    crashed container's last line is often a harmless startup notice, so only lines that read as errors count)."""
    for sig in comp.get("log_signals") or []:
        if sig.get("state") != "stopped" and sig.get("line") and not TELEMETRY_RE.search(sig["line"]):
            return quote(sig["line"])
    for lines in (comp.get("previous_container_logs") or {}).values():
        for line in reversed(lines or []):
            if is_error_line(line) and not TELEMETRY_RE.search(line):
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


def failing_callers(snapshot: ClusterSnapshot, cid: str) -> dict[str, dict[str, int]]:
    """Callers of `cid`, from the error lines that name it and from the addresses callers are configured with: a
    caller that addresses `cid` counts only while it logs dependency errors (connection, timeout, DNS, login)."""
    comp = snapshot.components.get(cid) or {}
    out = {src: dict(classes) for src, classes in (comp.get("referenced_by_errors_from") or {}).items()}
    for other_id, other in snapshot.components.items():
        if other_id == cid or other_id in out or cid not in (other.get("_calls") or []):
            continue
        classes: dict[str, int] = {}
        for f in other.get("log_findings") or []:
            for klass in f.get("classes") or []:
                if klass in DEPENDENCY_CLASSES:
                    classes[klass] = classes.get(klass, 0) + int(f.get("count") or 1)
        if classes:
            out[other_id] = classes
    return out


def users_of(snapshot: ClusterSnapshot, fo: dict) -> list[str]:
    return [c for c in fo.get("components") or [] if c in snapshot.components]


def feedback_path(snapshot: ClusterSnapshot, d: Diagnosis) -> dict | None:
    """The call path the investigation located the fault on (the decision model chose it), unless a ConfigMap or
    Secret holding the settings is the fault object, which then is the subject itself."""
    fo = d.fault_object
    if fo and fo["kind"] in CONFIG_KINDS:
        return None
    return d.call_path


RUN_TO_COMPLETION = ("Job", "CronJob", "Pod")
BROKERS = {"kafka": "Kafka", "redpanda": "Redpanda", "pulsar": "Pulsar", "nats": "NATS", "rabbitmq": "RabbitMQ"}


def driven_target(snapshot: ClusterSnapshot, d: Diagnosis) -> str | None:
    """A Job (or other run-to-completion workload) concluded as the cause that calls a component failing on its
    own: the failing component is the broken thing and leads the report, the Job is named as its driver."""
    comp = snapshot.components.get(d.component) or {}
    if comp.get("kind") not in RUN_TO_COMPLETION or d.fault_object:
        return None
    failing = [
        c
        for c in comp.get("_calls") or []
        if c in snapshot.components and "symptoms" in (snapshot.components[c].get("evidence_kinds") or [])
    ]
    return failing[0] if len(failing) == 1 else None


def broker_technology(snapshot: ClusterSnapshot) -> str | None:
    """The message broker the application runs, from its workloads' names and images."""
    for comp in snapshot.components.values():
        text = " ".join([comp.get("name") or "", *[str(c.get("image") or "") for c in comp.get("containers") or []]])
        for key, label in BROKERS.items():
            if key in text.lower():
                return label
    return None


def path_phrase(snapshot: ClusterSnapshot, path: dict) -> str:
    caller, callee = snapshot.components[path["caller"]], snapshot.components[path["callee"]]
    where = f" in namespace `{callee['namespace']}`" if caller["namespace"] == callee["namespace"] else ""
    return (
        f"the call path from {describe(caller['kind'], caller['name'], None if where else caller['namespace'])} to "
        f"{describe(callee['kind'], callee['name'], None if where else callee['namespace'])}{where}"
    )


@dataclass
class Cause:
    """Where the fault lives, as the report states it: `subject` holds the bad value or state, `related` names the
    other object with its relation, `fact` says what is wrong, `mechanism` holds the lines this relation adds, and
    `broken` is the component whose state, effects and callers are reported."""

    subject: str
    fact: str | None = None
    related: str | None = None
    mechanism: list[str] = field(default_factory=list)
    broken: dict | None = None
    own_error: bool = True  # the concluded component's own error line belongs in the mechanism


def blocked_input_cause(snapshot: ClusterSnapshot, d: Diagnosis, comp: dict) -> Cause | None:
    """One input item keeps failing and blocks its consumer: the input, named by its value (a topic when its env
    name or the broker says so), is the subject."""
    b = d.blocked_input or {}
    source = b.get("topic") or b.get("input")
    if not comp or not source:
        return None
    env = re.match(r"env (\S+?)=(.+)", str(source))
    value, via = (env.group(2), env.group(1)) if env else (str(source), None)
    topic = bool(b.get("topic")) or bool(via and "topic" in via.lower())
    noun = f"{broker_technology(snapshot) or ''} topic".strip() if topic else "input"
    named = f" (named by env `{via}`)" if via else ""
    return Cause(subject=f"the {noun} `{value}`{named} consumed by {_where(comp)}", broken=comp)


def call_path_cause(snapshot: ClusterSnapshot, d: Diagnosis, comp: dict) -> Cause | None:
    """A caller retries timed-out calls into a callee that caps its throughput: the call path is the subject."""
    path = None if d.blocked_input else feedback_path(snapshot, d)
    if path is None:
        return None
    names = {cid: c.get("name") or cid for cid, c in snapshot.components.items()}
    return Cause(
        subject=path_phrase(snapshot, path),
        fact=paths.describe(path, names),
        mechanism=[f'{names[path["caller"]]} logs "{quote(path["example"])}"'],
        broken=comp,
        own_error=d.component != path["caller"],
    )


def driver_cause(snapshot: ClusterSnapshot, d: Diagnosis, comp: dict) -> Cause | None:
    """A run-to-completion workload calls one component that fails on its own: the failing component is the
    subject and the workload its driver; the workload's command and decoded script explain how."""
    target = None if d.blocked_input else driven_target(snapshot, d)
    if target is None:
        return None
    broken = snapshot.components[target]
    state = [x for x in broken.get("signals") or [] if STATE_WORDS.search(x)]
    own = [x for x in comp.get("signals") or [] if not x.startswith(_POINTERS)]
    mechanism = [
        f"{describe(comp['kind'], comp['name'], None)} calls {broken['name']}"
        + (f" ({own[0].rstrip('.')})" if own else "")
    ]
    scripts = [
        " ".join(text.split())
        for ct in comp.get("containers") or []
        for arg in [*(ct.get("command") or []), *(ct.get("args") or [])]
        for text in checks.decoded_payloads(str(arg))
    ]
    if scripts:
        mechanism.append(f"the script it runs (decoded from base64): {scripts[0][: QUOTE_CHARS * 2]}")
    return Cause(
        subject=f"{component_phrase(broken, both_kinds=True)}, driven by {describe(comp['kind'], comp['name'], None)}, "
        "which calls it",
        fact=next((x for x in state if re.search(r"(?i)OOMKilled|limit|evicted", x)), state[0] if state else None),
        mechanism=mechanism,
        broken=broken,
        own_error=False,
    )


def fault_object_cause(snapshot: ClusterSnapshot, d: Diagnosis, comp: dict) -> Cause | None:
    """Another object carries the bad value: a policy, webhook or outside object acting on the workload, a Service
    selecting it, or a ConfigMap/Secret (the object itself when shared, else the workload through it)."""
    fo = d.fault_object
    if not fo:
        return None
    phrase = describe(fo["kind"], fo["name"], fo.get("namespace"))
    if not comp:
        users = [snapshot.components[c] for c in users_of(snapshot, fo)][:MAX_AFFECTED]
        acted = ", ".join(_where(u) for u in users)
        return Cause(subject=f"{phrase}, acting on {acted}" if acted else phrase, broken=users[0] if users else None)
    outside = not fo.get("namespace") or fo.get("namespace") not in set(snapshot.app.get("namespaces") or [])
    if fo["kind"] in CARRIER_KINDS or fo["kind"] == "NetworkPolicy" or outside:
        return Cause(subject=f"{phrase}, acting on {_where(comp)}", broken=comp)
    if fo["kind"] == "Service":
        return Cause(subject=f"{phrase} (selecting {_where(comp)})", broken=comp)
    if fo["kind"] in CONFIG_KINDS:
        users = users_of(snapshot, fo)
        if len(users) > 1:
            return Cause(subject=f"{phrase}, used by {', '.join(users)}", broken=comp)
        return Cause(
            subject=component_phrase(comp, both_kinds=True),
            related=f"through its {fo['kind']} `{fo['name']}`",
            broken=comp,
        )
    return None


def common_change_cause(snapshot: ClusterSnapshot, d: Diagnosis, comp: dict) -> Cause | None:
    """The same pod template change applied to several workloads at once: one cause naming all of them."""
    if not comp or not _has_change(comp):
        return None
    group = same_change(snapshot, d.component)
    if not group:
        return None
    names = ", ".join(f"`{snapshot.components[c]['name']}`" for c in [d.component, *group])
    return Cause(
        subject=f"{comp['kind']}s {names} in namespace `{comp['namespace']}` (the same change on {len(group) + 1})",
        broken=comp,
    )


def component_cause(snapshot: ClusterSnapshot, d: Diagnosis, comp: dict) -> Cause:
    """The workload itself (with its Service of the same name when nothing of it was changed)."""
    if not comp:
        return Cause(subject=f"`{d.component}`")
    return Cause(subject=component_phrase(comp, both_kinds=not _has_change(comp)), broken=comp)


# The relation kinds a cause can have, most specific first; the first that applies names the cause.
RELATIONS = (
    blocked_input_cause,
    call_path_cause,
    driver_cause,
    fault_object_cause,
    common_change_cause,
    component_cause,
)


def locate(snapshot: ClusterSnapshot, d: Diagnosis) -> Cause:
    comp = snapshot.components.get(d.component) or {}
    return next(c for c in (rule(snapshot, d, comp) for rule in RELATIONS) if c is not None)


def _where(comp: dict) -> str:
    return describe(comp["kind"], comp["name"], comp["namespace"])


def _has_change(comp: dict) -> bool:
    return "change" in (comp.get("evidence_kinds") or []) or bool(changed_values(comp))


def blocked_mechanism(b: dict) -> str:
    if b.get("source") == "broker":
        return f"consumer group {b.get('group')} is stalled at offset {b.get('offset')} of topic {b.get('topic')}"
    evidence = f": {b['evidence']}" if b.get("evidence") else ""
    return f"one input item fails processing again and again and blocks the items behind it{evidence}"


def build_report(snapshot: ClusterSnapshot, d: Diagnosis) -> str:
    text = cause_block(snapshot, d)
    if d.second is not None:
        text += "\n\nA second, independent root cause:\n" + cause_block(snapshot, d.second)
    return text


def cause_block(snapshot: ClusterSnapshot, d: Diagnosis) -> str:
    """Root cause, Mechanism, Details, Effect, Observed, Affected for one cause."""
    comp = snapshot.components.get(d.component) or {}
    fo = d.fault_object
    cause = locate(snapshot, d)
    changes = changed_values(comp)
    fact = cause.fact or key_fact(d) or (fo or {}).get("signal") or (changes[0] if changes else None)
    if fact is None:
        fact = next((s for s in comp.get("signals") or [] if not STATE_WORDS.search(s)), None)
    head = f"Root cause: {cause.subject}" + (f", {cause.related}" if cause.related else "")
    lines = [head + (f": {fact.rstrip('.')}." if fact else ".")]

    rejecting = [
        (sid, s)
        for sid, s in snapshot.components.items()
        if d.component in (s.get("login_candidates") or []) and sid != d.component
    ]
    login = next((x for x in comp.get("signals") or [] if x.startswith("server ") and "rejects logins" in x), None)
    lead = blocked_mechanism(d.blocked_input) if d.blocked_input and comp else (login or (fo or {}).get("signal"))
    parts = [m.rstrip(".") for m in [lead, *cause.mechanism] if m and m != fact]
    error = own_error_line(comp) if comp and cause.own_error else None
    if error and (not fact or error not in fact):
        parts.append(f'the component logs "{error}"')
    if not parts and comp:
        # no error line: the component's own signal that says why, never a repeat of the root-cause line
        own = [x for x in comp.get("signals") or [] if x != fact and not x.startswith(_POINTERS)]
        plain = [x for x in own if not STATE_WORDS.search(x)]  # state lines belong to the Effect line
        pick = next((x for x in own if _CAUSAL.search(x)), plain[0] if plain else None)
        if pick:
            parts.append(pick.rstrip("."))
    if parts:
        lines.append("Mechanism: " + "; ".join(dict.fromkeys(parts)) + ".")

    details = [c for c in changes if c != fact]
    for step in (d.investigation or {}).get("steps") or []:
        if step.get("component") == d.component:
            details += [x for x in step.get("spec_checks") or [] if x != fact]
    details = list(dict.fromkeys(x.rstrip(".") for x in details if x))[:MAX_DETAILS]
    if details:
        lines.append("Details: " + "; ".join(details) + ".")

    broken = cause.broken or {}
    broken_id = next((c for c, x in snapshot.components.items() if x is broken), d.component)
    callers = failing_callers(snapshot, broken_id) if broken else {}
    if broken:
        shown = "\n".join(lines)
        effects = [
            s.rstrip(".") for s in broken.get("signals") or [] if STATE_WORDS.search(s) and s.rstrip(".") not in shown
        ]
        effects = effects[:2]
        classes: dict[str, int] = {}
        for by_class in callers.values():
            for klass, n in by_class.items():
                classes[klass] = classes.get(klass, 0) + n
        if classes:
            seen = ", ".join(f"{k} ({n} lines)" for k, n in sorted(classes.items(), key=lambda kv: -kv[1])[:3])
            effects.append(f"its callers see {seen}")
        if effects:
            lines.append(f"Effect: {'; '.join(effects)}.")
        # the cause's other own facts, so every part involved (containers, jobs, accounts) is named
        shown = "\n".join(lines)
        observed = [x.rstrip(".") for x in broken.get("signals") or [] if not x.startswith(_POINTERS)]
        observed = [x for x in dict.fromkeys(observed) if x not in shown][:MAX_OBSERVED]
        if observed:
            lines.append("Observed: " + "; ".join(observed) + ".")

    affected: list[str] = []
    for _, server in rejecting[:MAX_AFFECTED]:
        why = next((x for x in server.get("signals") or [] if "rejects logins" in x), "rejects its logins")
        affected.append(f"{describe(server['kind'], server['name'], server.get('namespace'))}: {why.rstrip('.')}")
    for src, by_class in sorted(callers.items(), key=lambda kv: -sum(kv[1].values()))[:MAX_AFFECTED]:
        other = snapshot.components.get(src) or {}
        name = describe(other.get("kind", "component"), other.get("name", src), other.get("namespace"))
        klass = ", ".join(sorted(by_class))
        affected.append(f"{name}: {klass} calling it ({sum(by_class.values())} lines, still occurring)")
    if fo and not affected:
        others = [c for c in users_of(snapshot, fo) if c != d.component][:MAX_AFFECTED]
        affected += [describe(snapshot.components[c]["kind"], snapshot.components[c]["name"], None) for c in others]
    affected = [a for a in affected if a.split(":", 1)[0] not in lines[0]]
    if affected:
        lines.append("Affected (symptoms only, not causes): " + "; ".join(affected) + ".")
    return "\n".join(lines)
