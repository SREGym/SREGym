"""Explanation builders: generic patterns filled in from the jev_diag snapshot (after its post-processing).

Nothing here names a fault. Each builder reads structures the team's collector already derives: pod template
diffs per workload, change records with the components they act on, cluster findings, and the components that
other components' still-occurring errors name.
"""

from __future__ import annotations

import re
from typing import Any

from clients.jev_diag.derive import CHANGE_MARKERS, OWN_LOG_MARKERS, POINTED_MARKERS, SYMPTOM_MARKERS
from clients.jev_diag.timeutil import parse_time, seconds_between
from clients.jev_diag.v2.explain import CONTRADICTS, PARTIAL, SUPPORTS, UNKNOWN, Explanation, Prediction

ONSET_TOLERANCE_S = 10  # the onset estimate is coarse; a change this soon after it still counts as before
TIMEOUT_CLASSES = {"connectivity or timeout", "overload or quota exhaustion"}
AUTH_CLASS = "authentication failure"
DATA_CLASS = "message or data processing failure"
SATURATION_RE = re.compile(r"(?i)cpu|throttl|queue|overload|latency|limit|exhaust|429|too many|saturat|slow")
SETTING_RE = re.compile(r"(?i)retr|attempt|timeout|deadline|backoff|qps|rate|limit|concurren|pool|queue")
MAX_INPUTS = 4
POD_PREFIX_RE = re.compile(r"^pod [\w.-]+: ")
CONNECT_CLASS = "connectivity or timeout"
# The team's checks phrase findings about an object that acts on a workload from outside as "<kind> <name> ..."
# or "selected by <Kind> <name>" on the workload; such a finding is that object's evidence, not the workload's own.
OUTSIDE_RE = re.compile(
    r"(?i)^(?:selected by |coredns rule |(?:service account|service|networkpolicy|resourcequota|limitrange|"
    r"mutatingwebhookconfiguration|validatingwebhookconfiguration|clusterrole|role) [\w./-]+ )"
)
TIMEOUT_RE = re.compile(r"(?i)time ?out|timed out|deadline")
# The team's check for an object rewritten after a reader started that the reader only reads at startup.
STALE_RE = re.compile(r"^(\w+) ([\w.-]+) was rewritten .*still use the previous values")


def object_id(comp: dict) -> str:
    """'kind/namespace/name' for a snapshot component."""
    return f"{comp.get('kind', 'workload').lower()}/{comp.get('namespace')}/{comp.get('name')}"


def _healthy(comp: dict | None) -> bool:
    return comp is None or bool(comp.get("healthy", True))


def impact_of(components: dict[str, dict], cid: str) -> list[str]:
    """The component itself when it shows symptoms, plus callers with symptoms whose errors name it."""
    comp = components.get(cid)
    if comp is None:
        return []
    out = [] if _healthy(comp) else [object_id(comp)]
    for src in sorted(comp.get("referenced_by_errors_from") or {}):
        if src in components and not _healthy(components[src]):
            out.append(object_id(components[src]))
    return out


def findings_about(components: dict[str, dict], kind: str, name: str, limit: int = 3) -> list[str]:
    """The team's code-check signals that name an object (e.g. "service frontend selects pods of ..."), skipping
    plain change notices, which the change record already carries."""
    needle = f"{kind.lower()} {name}".lower()
    out: list[str] = []
    for comp in components.values():
        for sig in comp.get("signals") or []:
            low = sig.lower()
            if needle in low and " was modified " not in low and " was created " not in low and sig not in out:
                out.append(sig)
    return out[:limit]


def named_by(components: dict[str, dict], kind: str, name: str) -> list[str]:
    """Components with a check finding that starts by naming the object ("service frontend selector ...")."""
    head = f"{kind.lower()} {name} ".lower()
    return [
        cid
        for cid, comp in components.items()
        if any(sig.lower().startswith(head) for sig in comp.get("signals") or [])
    ]


def timing(onset: Any, at: Any, what: str) -> Prediction:
    """Whether a change came before the first symptom: supports, contradicts (it came after), or unknown."""
    before = seconds_between(parse_time(onset), parse_time(at)) if onset and at else None
    # Only ever support: the onset estimate can predate a change because of startup noise, so "after the
    # symptoms" is not evidence against a change.
    status = SUPPORTS if before is not None and before >= -ONSET_TOLERANCE_S else UNKNOWN
    return Prediction(f"{what} was made before the symptoms began", status, group=f"timing:{what}")


def own_symptoms(comp: dict | None, name: str) -> Prediction:
    status = UNKNOWN if comp is None else (CONTRADICTS if _healthy(comp) else SUPPORTS)
    return Prediction(f"{name} shows symptoms of its own", status, group=f"symptoms:{name}")


def change_explanations(snapshot: Any) -> list[Explanation]:
    """One explanation per changed pod-template field, one for the fields that changed together, and one per
    other recorded object change that acts on a component with symptoms."""
    components, cluster = snapshot.components, snapshot.cluster
    onset = cluster.get("symptom_onset")
    out: list[Explanation] = []
    for cid, comp in components.items():
        tc = comp.get("template_changes") or {}
        diffs = [d for d in tc.get("changes") or [] if d and "pod-template-hash" not in d]  # changes on every rollout
        what = f"revision {tc.get('revision')} of {cid}"
        common = [timing(onset, tc.get("changed_at"), what), own_symptoms(comp, cid)]
        for d in diffs:
            out.append(
                Explanation(
                    key=f"change:{cid}:{d}",
                    pattern="change",
                    origin=object_id(comp),
                    mechanism=f"The pod template change '{d}' in {what} breaks {cid}.",
                    values=[d],
                    impact=impact_of(components, cid),
                    predictions=[Prediction(p.text, p.status, p.group) for p in common],
                )
            )
        if len(diffs) > 1:
            out.append(
                Explanation(
                    key=f"change:{cid}:together",
                    pattern="change",
                    origin=object_id(comp),
                    mechanism=f"The pod template changes in {what}, which only cause the fault together, break {cid}.",
                    values=diffs,
                    impact=impact_of(components, cid),
                    predictions=[Prediction(p.text, p.status, p.group) for p in common],
                )
            )
    for ch in cluster.get("_changes") or []:
        affected = [c for c in ch.get("affects") or [] if c in components and not _healthy(components[c])]
        if not affected or ch.get("role") == "observability":
            continue
        kind, name, ns = (ch.get("kind") or "object").lower(), ch.get("name"), ch.get("namespace")
        origin = f"{kind}/{ns}/{name}" if ns else f"{kind}/{name}"
        verb = "changed" if ch.get("modified_after_creation_s") is not None else "created"
        out.append(
            Explanation(
                key=f"change:{origin}",
                pattern="change" if ch.get("object") in affected else "acts-on",
                origin=origin,
                mechanism=f"{kind} {name} was {verb} recently and breaks {', '.join(affected)}, which it acts on.",
                values=([f"{kind} {name} fields written: {', '.join(ch['fields'])}"] if ch.get("fields") else [])
                + findings_about(components, kind, name),
                impact=sorted({i for c in affected for i in impact_of(components, c)}),
                predictions=[timing(onset, ch.get("at"), origin)] + [own_symptoms(components[c], c) for c in affected],
            )
        )
    return out


def acts_on_explanations(snapshot: Any) -> list[Explanation]:
    """Cluster-level objects the team's checks found acting on components (webhooks, quotas, DNS rules, ...)."""
    components = snapshot.components
    out: list[Explanation] = []
    for f in snapshot.cluster.get("findings") or []:
        kind, name, ns = (f.get("kind") or "object").lower(), f.get("name"), f.get("namespace")
        origin = f"{kind}/{ns}/{name}" if ns else f"{kind}/{name}"
        targets = [c for c in f.get("components") or [] if c in components]
        targets += [c for c in named_by(components, kind, name) if c not in targets]
        linked = [
            Prediction(f"the checks link {kind} {name} to {', '.join(targets)}", SUPPORTS, group=f"linked:{origin}")
        ]
        out.append(
            Explanation(
                key=f"acts-on:{origin}:{f.get('signal', '')[:60]}",
                pattern="acts-on",
                origin=origin,
                mechanism=f"{kind} {name} acts on {', '.join(targets) or 'the application'}: {f.get('signal')}.",
                values=([str(f.get("signal"))] if f.get("signal") else []) + findings_about(components, kind, name),
                impact=sorted({i for c in targets for i in impact_of(components, c)}),
                predictions=[own_symptoms(components[c], c) for c in targets] + (linked if targets else []),
            )
        )
    return out


def dependency_explanations(snapshot: Any) -> list[Explanation]:
    """For each component that other components' still-occurring errors name: it fails or rejects them."""
    components = snapshot.components
    callers: dict[str, list[str]] = {}
    for cid, comp in components.items():
        for target in comp.get("errors_point_to") or []:
            if target in components and target != cid:
                callers.setdefault(target, []).append(cid)
    out: list[Explanation] = []
    for target, srcs in sorted(callers.items()):
        tcomp = components[target]
        names = ", ".join(sorted(srcs))
        healthy = _healthy(tcomp)
        out.append(
            Explanation(
                key=f"dependency:{target}",
                pattern="dependency",
                origin=object_id(tcomp),
                mechanism=f"{target} fails or rejects requests, so {names} fail when calling it.",
                contributing=[],
                impact=sorted(
                    {object_id(components[s]) for s in srcs} | ({object_id(tcomp)} if not healthy else set())
                ),
                predictions=[
                    Prediction(f"still-occurring errors of {names} name {target}", SUPPORTS, group=f"errors:{target}"),
                    Prediction(
                        f"{target} shows failures of its own",
                        UNKNOWN if healthy else SUPPORTS,
                        group=f"symptoms:{target}",
                        check="probe" if healthy else None,  # a healthy-looking server may still reject clients
                    ),
                ],
            )
        )
    return out


def _caller_classes(comp: dict) -> dict[str, set[str]]:
    """caller id -> error classes in that caller's still-occurring errors that name `comp`."""
    return {src: set(classes or {}) for src, classes in (comp.get("referenced_by_errors_from") or {}).items()}


def _settings(details: dict[str, dict], cid: str) -> list[str]:
    """Env settings of `cid` that govern calls (retries, timeouts, limits), from its collected detail."""
    workload = (details.get(cid) or {}).get("workload") or {}
    out = []
    for c in (workload.get("containers") or []) + (workload.get("init_containers") or []):
        for e in c.get("env") or []:
            if SETTING_RE.search(str(e.get("name") or "")):
                out.append(f"{cid} {e.get('name')}={e.get('value', e.get('from', '?'))}")
    return out


def amplify_explanations(snapshot: Any, details: dict[str, dict]) -> list[Explanation]:
    """A caller whose errors on a callee are timeouts or overload: retries and deadlines may keep the callee
    saturated, a fault of the interaction rather than of either side alone."""
    components = snapshot.components
    out: list[Explanation] = []
    for target, tcomp in components.items():
        for src, classes in sorted(_caller_classes(tcomp).items()):
            if src not in components or not classes & TIMEOUT_CLASSES:
                continue
            scomp = components[src]
            saturated = any(SATURATION_RE.search(sig) for sig in tcomp.get("signals") or [])
            ns = tcomp.get("namespace")
            out.append(
                Explanation(
                    key=f"amplify:{src}->{target}",
                    pattern="amplify",
                    origin=f"interaction/{ns}/{scomp.get('name')}--{tcomp.get('name')}",
                    mechanism=(
                        f"Calls from {src} to {target} time out; {src} retries them, and the retries add load that "
                        f"keeps {target} saturated, so the failure sustains itself."
                    ),
                    values=_settings(details, src) + _settings(details, target),
                    contributing=[object_id(scomp), object_id(tcomp)],
                    impact=sorted({object_id(scomp)} | ({object_id(tcomp)} if not _healthy(tcomp) else set())),
                    predictions=[
                        Prediction(
                            f"errors of {src} on {target} are timeouts or overload", SUPPORTS, group=f"errors:{target}"
                        ),
                        Prediction(
                            f"{target} is saturated",
                            SUPPORTS if saturated else UNKNOWN,
                            group=f"saturation:{target}",
                            check=None if saturated else "metrics",
                        ),
                    ],
                )
            )
    return out


def hidden_state_explanations(snapshot: Any, details: dict[str, dict]) -> list[Explanation]:
    """Clients fail to authenticate against a server: the server's running settings may no longer match theirs."""
    components = snapshot.components
    out: list[Explanation] = []
    for target, tcomp in components.items():
        callers = sorted(src for src, classes in _caller_classes(tcomp).items() if AUTH_CLASS in classes)
        refused = not callers and _refused_without_own_trouble(tcomp)
        if refused:  # callers cannot connect to a server that shows no trouble of its own
            callers = sorted(src for src, classes in _caller_classes(tcomp).items() if CONNECT_CLASS in classes)
        if not callers:
            continue
        names = ", ".join(callers)
        changed = bool((tcomp.get("template_changes") or {}).get("changes"))
        out.append(
            Explanation(
                key=f"hidden-state:{target}",
                pattern="hidden-state",
                origin=object_id(tcomp),
                mechanism=(
                    f"{target}'s running authentication settings no longer match the credentials {names} present, "
                    f"so their requests are rejected although {target} itself keeps running."
                ),
                impact=sorted({object_id(components[c]) for c in callers if c in components}),
                predictions=[
                    Prediction(
                        f"{names} get authentication failures from {target}",
                        UNKNOWN if refused else SUPPORTS,
                        group=f"auth:{target}",
                    ),
                    Prediction(
                        f"{target} shows no trouble of its own",
                        SUPPORTS if refused else UNKNOWN,
                        group=f"quiet:{target}",
                    ),
                    Prediction(
                        f"{target}'s configuration was not changed",
                        CONTRADICTS if changed else SUPPORTS,
                        group=f"config:{target}",
                    ),
                    Prediction(f"a login check against {target} is rejected", UNKNOWN, check="probe"),
                ],
            )
        )
    return out


def _refused_without_own_trouble(comp: dict) -> bool:
    """Only other components' errors point at it (no symptoms, changes or settings of its own), and those errors are
    failed connections rather than timeouts or overload."""
    if set(comp.get("evidence_kinds") or []) != {"pointed_at"}:
        return False
    examples = [sig.split("e.g.", 1)[-1] for sig in comp.get("signals") or []]  # the quoted error, not its class
    return not any(TIMEOUT_RE.search(x) or SATURATION_RE.search(x) for x in examples)


def stale_read_explanations(snapshot: Any) -> list[Explanation]:
    """A workload still running with values it read at startup from an object rewritten after it started (the
    team's check): the workload is the origin, the rewritten object contributes."""
    components = snapshot.components
    out: list[Explanation] = []
    for cid, comp in components.items():
        sigs = comp.get("signals") or []
        for sig in sigs:
            m = STALE_RE.match(sig)
            if not m:
                continue
            oid = object_id(comp)
            source = f"{m.group(1).lower()}/{comp.get('namespace')}/{m.group(2)}"
            rejected = [x for x in sigs if x.startswith("server ") and "rejects" in x]
            out.append(
                Explanation(
                    key=f"stale-read:{cid}:{source}",
                    pattern="hidden-state",
                    origin=oid,
                    mechanism=f"{oid} still runs with the values it read at startup: {sig}.",
                    contributing=[source],
                    values=[sig, *rejected[:1]],
                    impact=impact_of(components, cid),
                    predictions=[
                        own_symptoms(comp, oid),
                        Prediction(
                            f"a server rejects what {oid} presents",
                            SUPPORTS if rejected else UNKNOWN,
                            group=f"rejected:{cid}",
                        ),
                    ],
                )
            )
    return out


def bad_input_explanations(snapshot: Any, details: dict[str, dict]) -> list[Explanation]:
    """A consumer failing on its input again and again: one bad item in an input it reads blocks the rest. One
    explanation per input candidate its env names; the model chooses, code never guesses from variable names."""
    components = snapshot.components
    out: list[Explanation] = []
    for cid, comp in components.items():
        detail = details.get(cid) or {}
        repeated = (detail.get("logs") or {}).get("repeated_failures") or []
        own = any(DATA_CLASS in sig for sig in comp.get("signals") or [])
        if not (repeated or own):
            continue
        for cand in (detail.get("input_candidates") or [])[:MAX_INPUTS]:
            value = cand.get("value")
            out.append(
                Explanation(
                    key=f"bad-input:{cid}:{value}",
                    pattern="bad-input",
                    origin=f"input/{comp.get('namespace')}/{value}",
                    mechanism=f"One bad item in {value}, which {cid} reads, fails again and again and blocks the "
                    "items behind it.",
                    values=[f"{cid} reads {cand.get('env')}={value}"] + [r.get("line", "")[:200] for r in repeated[:1]],
                    impact=impact_of(components, cid),
                    predictions=[
                        Prediction(
                            f"{cid} fails repeatedly on the same item",
                            SUPPORTS if repeated else UNKNOWN,
                            group=f"repeat:{cid}",
                        ),
                    ],
                )
            )
    return out


def own_settings(comp: dict) -> list[str]:
    """The team's checks that found an abnormal setting of the component's own (its `configuration` evidence),
    without alerts, symptoms, changes or other components' errors."""
    out: list[str] = []
    for s in comp.get("signals") or []:
        if (
            s.startswith(("firing alert", *POINTED_MARKERS, *OWN_LOG_MARKERS))
            or any(m in s for m in (*CHANGE_MARKERS, *SYMPTOM_MARKERS))
            or OUTSIDE_RE.match(s)
        ):
            continue
        s = POD_PREFIX_RE.sub("", s)  # the same finding on every replica counts once
        if s not in out:
            out.append(s)
    return out


def own_setting_explanations(snapshot: Any, covered: set[str]) -> list[Explanation]:
    """'X's own setting is wrong' for a component with symptoms and such a check, unless another object already
    explains it (`covered`: what change and acts-on chains of other objects impact). Observability components count too."""
    out: list[Explanation] = []
    for cid, comp in snapshot.components.items():
        oid = object_id(comp)
        own = own_settings(comp)[:3]
        if _healthy(comp) or not own or oid in covered:
            continue
        out.append(
            Explanation(
                key=f"own-setting:{cid}",
                pattern="own-setting",
                origin=oid,
                mechanism=f"{oid} has a wrong setting of its own: {'; '.join(own)}",
                values=own,
                impact=impact_of(snapshot.components, cid),
                predictions=[own_symptoms(comp, oid)],
            )
        )
    return out


def partial_explanations(snapshot: Any) -> list[Explanation]:
    """'The fault is in X, mechanism unknown' for every component with symptoms."""
    return [
        Explanation(
            key=f"partial:{cid}",
            pattern=PARTIAL,
            origin=object_id(comp),
            mechanism="The fault is in this component; the mechanism is not established.",
            impact=impact_of(snapshot.components, cid),
        )
        for cid, comp in snapshot.components.items()
        if not _healthy(comp) and comp.get("role") != "observability"
    ]


def build_explanations(snapshot: Any, details: dict[str, dict] | None = None) -> list[Explanation]:
    """Every explanation the patterns produce, de-duplicated by key, in a stable order. `details` holds the
    component detail read so far (cid -> detail); patterns that need it skip components without it."""
    details = details or {}
    seen: dict[str, Explanation] = {}
    builders = [
        change_explanations(snapshot),
        acts_on_explanations(snapshot),
        dependency_explanations(snapshot),
        amplify_explanations(snapshot, details),
        hidden_state_explanations(snapshot, details),
        stale_read_explanations(snapshot),
        bad_input_explanations(snapshot, details),
    ]
    covered = {o for group in builders[:2] for e in group for o in e.impact if o != e.origin}
    builders += [own_setting_explanations(snapshot, covered), partial_explanations(snapshot)]
    acts_on: dict[str, Explanation] = {}
    for group in builders:
        for e in group:
            if e.pattern == "acts-on" and e.origin in acts_on:  # one object, found by a change and by a check
                first = acts_on[e.origin]
                first.values += [v for v in e.values if v not in first.values]
                first.impact = sorted(set(first.impact) | set(e.impact))
                groups = {p.group for p in first.predictions}
                first.predictions += [p for p in e.predictions if p.group not in groups]
                continue
            if e.pattern == "acts-on":
                acts_on[e.origin] = e
            seen.setdefault(e.key, e)
    return list(seen.values())
