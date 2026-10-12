"""Evidence kinds per origin candidate, computed from the notebook (the generic part of jev_diag's triage).

jev_diag's component question tags each candidate with `evidence_kinds` (change, configuration, pointed_at,
own_failure_logs, symptoms, error_logs_only) and gives the model a triage order over those kinds. The kinds are
generic categories of evidence, not fault detectors; here they are derived from what the investigation actually
observed, so the decision model chooses among candidates by what each one is known for instead of
cross-referencing dozens of observations itself.

Kinds:
  stale_config      its running containers hold values from before a write to a ConfigMap or Secret they read into
                    environment variables (the write was made, the component did not pick it up), and a failure goes
                    with it: a server reports failed logins, or the component itself fails. Writes to configuration
                    are routine (certificate renewals, operators), so a stale value with no failure is only a change.
  change            its own spec, Service, ConfigMap or Secret was written shortly before the symptoms began (within
                    the change window, default 30 min) or since, or its running pods differ from its template (set at
                    admission). Older writes are history: a long-running application has many of them, and they did
                    not start the symptoms. Writes from before the application was deployed never count.
  configuration     an abnormal setting of its own: a Service selecting no or several workloads, no ready endpoints,
                    or ready pods still running an older pod template whose settings the desired one changed
  cluster_finding   (cluster objects) an abnormal webhook, quota, policy, RBAC or DNS finding
  input_stalled     (input data) its consumer's committed offset stays fixed on one record while new records arrive,
                    and the consumer's own errors name that offset or report a data-processing failure (a stall with
                    no such error can as well be a slow or wedged consumer, so it is only a symptom); or, with any
                    queue or broker, the consumer's errors repeat on one and the same message (an unchanged offset,
                    id, sequence or delivery tag) or keep failing to parse what it reads, and are not connection or
                    authentication failures
  pointed_at        ongoing errors of other components name it
  own_failure_logs  its own ongoing errors (or a crash's last words) report authentication, authorisation or
                    data-processing failures
  symptoms          restarts, Pending, not ready, OOM, saturation, failing probes
  error_logs_only   ongoing errors of its own with none of the above
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from clients.jev_agent.notebook import Notebook, named_in
from clients.jev_agent.types import AgentContext

KINDS_ORDER = (
    "stale_config",
    "change",
    "configuration",
    "cluster_finding",
    "input_stalled",
    "pointed_at",
    "own_failure_logs",
    "symptoms",
    "error_logs_only",
)
CHANGE_RE = re.compile(
    r"written again|revision \d+ change:|differs from its \w+ template|^fields written|changed after deployment|"
    r"was written \d+s after container|was rewritten in the same second as container"
)
CONFIG_RE = re.compile(r"selector matches 0 pod|ready 0\b|selects pods of (several|multiple)|matches no workload")
# describe: ready pods still run an older pod template whose settings the desired one changed (stuck rollout)
OLD_TEMPLATE_RE = re.compile(r"pods of the older pod template .* still serve")
# Login failures reported by a server: its clients' credentials are then worth checking.
LOGIN_FAILURE_RE = re.compile(
    r"(?i)auth(entication)? failed|password|login|credential|access denied|unauthori|noauth|wrongpass|permission denied"
)
# Settings of a caller that name a dependency but only locate it (not how it is called).
_LOCATOR_SUFFIX = re.compile(r"(?i)_(addr|address|host|hostname|port|url|uri|endpoint|name|service|svc)$")
AUTH_DATA_RE = re.compile(
    r"(?i)\bauth|denied|forbidden|unauthori|permission|password|credential|noauth|wrongpass|invalid|malformed|"
    r"parse|decode|deserial|corrupt|schema|unexpected (eof|token|end)"
)
SYMPTOM_RE = re.compile(
    r"(?i)restarts=[1-9]|restarts\[[^\]]+\]: total [1-9]|Pending|CrashLoopBackOff|OOMKilled|ready=False|not ready|"
    r"failed [1-9]\d*/\d+ samples|\(max \d{2,3}% of it\)|recent \d+% of it|Unschedulable|queue_depth|throttled|ReplicaFailure=True|Warning FailedCreate"
)


def _is_change(line: str) -> bool:
    """A line reporting a change that can have started the symptoms (the tools mark writes that are cluster history
    or came too long before the symptoms; see `write_note`)."""
    return bool(CHANGE_RE.search(line)) and PREDATES_MARK not in line and TOO_EARLY_MARK not in line


# Error classes (labelled by the decision model, policy.ERROR_CLASSES) that report a failure of the component
# itself rather than of something it calls; unlabelled lines fall back to AUTH_DATA_RE.
OWN_FAILURE_CLASSES = {"authentication", "data", "configuration"}


def _own_failure(notebook: Notebook, line: str) -> bool:
    """Whether an error line reports a failure of the component itself (authentication, data or configuration), by
    the decision model's label or, unlabelled, by its wording."""
    label = notebook.error_labels.get(line)
    return label in OWN_FAILURE_CLASSES if label else bool(AUTH_DATA_RE.search(line))


STALE_RE = re.compile(r"still holds? the values from before the write")
STALLED_RE = re.compile(r"committed offset (\d+) unchanged .* log end moved")
STALLED_CONSUMER_RE = re.compile(r"; consumer (\S+/\S+)")
# Errors a consumer reports about the data it reads (not about connecting or authenticating).
DATA_ERROR_RE = re.compile(
    r"(?i)pars(e|ing)|decod|deserial|corrupt|malformed|schema|invalid (json|record|message|payload|format)|"
    r"unexpected (eof|token|end)"
)
MAX_FACTS = 2
FACT_CHARS = 180


def _comps(ctx: AgentContext) -> dict:
    """The snapshot's components by id ({} when it has none)."""
    comps = getattr(ctx.snapshot, "components", None) or {}
    return comps if isinstance(comps, dict) else {}


def _name(ctx: AgentContext, cid: str) -> str:
    """The component's name (the id's last part when it is unknown)."""
    return (_comps(ctx).get(cid) or {}).get("name") or cid.split("/")[-1]


def _token(name: str) -> re.Pattern:
    """A pattern matching `name` as a whole token (not part of a longer hyphenated name)."""
    return re.compile(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])")


# Objects that commonly share their name with a workload: a line counts for them only when it names the kind too
# ("ConfigMap jaeger", "Service frontend", "configmap/ns/x"), so a workload's facts are not credited to them.
_KIND_WORDS = {"configmap": "ConfigMap", "service": "Service", "secret": "Secret"}


def _object_pattern(kind: str, name: str) -> re.Pattern:
    """A pattern matching an object's name where a line names it as that kind ('ConfigMap x', 'configmap/ns/x'); a
    plain name token for other kinds."""
    if kind not in _KIND_WORDS:
        return _token(name)
    word = re.escape(_KIND_WORDS[kind])
    return re.compile(rf"(?i)(?:\b{word}s?\s+(?:[a-z0-9-]+/)?|\b{kind}/(?:[a-z0-9-]+/)?){re.escape(name)}(?![a-z0-9-])")


def _short(line: str) -> str:
    """A line with whitespace collapsed, cut to FACT_CHARS."""
    text = " ".join(str(line).split())
    return text if len(text) <= FACT_CHARS else text[: FACT_CHARS - 1] + "…"


CHANGE_WINDOW_S = 1800.0  # a write this long before the symptoms began is too early to have started them
PREDATES_MARK = "predates the application's deployment"
PREDATES_APP = f"; this write {PREDATES_MARK}, so it is not a change made to this deployment"
TOO_EARLY_MARK = "too early to have started them"


def change_window_s(ctx: AgentContext) -> float:
    """How long before the symptoms began a write still counts as a change (JEV_AGENT_CHANGE_WINDOW_S)."""
    try:
        return float((ctx.extra or {}).get("change_window_s") or CHANGE_WINDOW_S)
    except (TypeError, ValueError):
        return CHANGE_WINDOW_S


def symptom_onset(ctx: AgentContext):
    """When the first still-active symptom began (the overview's estimate), or None."""
    from clients.jev_diag.timeutil import parse_time

    return parse_time(((ctx.overview or {}).get("cluster") or {}).get("symptom_onset"))


def write_note(ctx: AgentContext, ts: str | None) -> str:
    """ "" for a write that can have started the symptoms; else a note saying why it cannot: it predates the
    application's deployment (cluster history), or it came more than the change window before the symptoms began
    (before now, when no symptom has been seen)."""
    from clients.jev_diag.changes import MIN_CHANGE_SECONDS
    from clients.jev_diag.timeutil import now, parse_time, seconds_between

    moment = parse_time(ts)
    if moment is None:
        return ""
    age, ago = app_age_s(ctx), seconds_between(now(), moment)
    if age is not None and ago is not None and ago > age + MIN_CHANGE_SECONDS:
        return PREDATES_APP
    onset = symptom_onset(ctx)
    before = seconds_between(onset or now(), moment)
    if before is not None and before > change_window_s(ctx):
        when = "before the symptoms began" if onset else "ago, with no symptom seen since"
        return f"; this write came {before // 60} min {when}, {TOO_EARLY_MARK}"
    return ""


def change_in_window(ctx: AgentContext, change: dict) -> bool:
    """Whether an overview change record (seconds_ago, seconds_before_symptoms) can have started the symptoms: made
    after the application was deployed, and no more than the change window before the symptoms began."""
    from clients.jev_diag.changes import MIN_CHANGE_SECONDS

    ago, before = change.get("seconds_ago"), change.get("seconds_before_symptoms")
    age = app_age_s(ctx)
    if age is not None and ago is not None and ago > age + MIN_CHANGE_SECONDS:
        return False
    lead = before if before is not None else ago
    return lead is None or lead <= change_window_s(ctx)


def app_age_s(ctx: AgentContext) -> float | None:
    """Seconds since the application's first workload was created (changes before that are not 'after deploy')."""
    from clients.jev_diag.timeutil import now, parse_time

    times = [parse_time(c.get("created")) for c in _comps(ctx).values() if isinstance(c, dict) and c.get("created")]
    times = [t for t in times if t is not None]
    return (now() - min(times)).total_seconds() if times else None


def _call_edges(obs: list) -> list[tuple[str, str]]:
    """(caller, named component) for every component an observation's lines name, the caller being its target's
    first end."""
    edges = []
    for o in obs:
        caller = o.target.split("->")[0].strip()
        for ln in o.lines:
            edges += [(caller, callee) for callee in named_in(ln)]
    return edges


def _cluster_object_kinds(obs: list, specific: set[str] | None, add: Callable[[str, str], None]) -> None:
    """Evidence for the generic cluster-object candidate: abnormal cluster_objects lines that name no listed
    object candidate."""
    # findings about an object that is itself a listed candidate belong to that candidate
    named = [_token(n) for n in specific or set()]
    for o in obs:
        if o.tool == "cluster_objects" and (o.abnormal or 0.0) >= 0.5:
            for ln in o.lines:
                if not any(rx.search(ln) for rx in named):
                    add("cluster_finding", ln)
                    break


def _workload_kinds(
    notebook: Notebook, key: str, obs: list, edges: list, kinds: dict[str, str], add: Callable[[str, str], None]
) -> None:
    """Evidence for a workload candidate: changes, configuration, its logs, symptoms, stale config, and callers
    whose errors name it."""
    name = key.split("/")[-1]
    stale_lines: list[str] = []
    for o in obs:
        ends = {p.strip() for p in o.target.split("->")}
        if key not in ends:
            continue
        for ln in o.lines:
            if STALE_RE.search(ln):  # its config was written, not it: weighed below
                stale_lines.append(ln)
                continue
            if o.tool in ("config_and_changes", "describe", "service_view") and _is_change(ln):
                add("change", ln)
            if (o.tool == "service_view" and CONFIG_RE.search(ln)) or (
                o.tool == "describe" and OLD_TEMPLATE_RE.search(ln)
            ):
                add("configuration", ln)
            if o.tool == "logs" and (ln.startswith("ongoing") or ln.startswith("previous run")):
                if _own_failure(notebook, ln):
                    add("own_failure_logs", ln)
                else:
                    add("error_logs_only", ln)
            if SYMPTOM_RE.search(ln) and (o.abnormal or 0.0) >= 0.5:
                add("symptoms", ln)
    if stale_lines:
        failing = login_failures(notebook) or {"own_failure_logs", "error_logs_only", "symptoms"} & set(kinds)
        add("stale_config" if failing else "change", stale_lines[0])
    callers = [a for a, b in edges if b == key and a != key]
    if callers:
        add("pointed_at", f"ongoing errors of {', '.join(sorted(set(callers))[:3])} name {name}")


def _data_kinds(
    notebook: Notebook, consumer: str, obs: list, kinds: dict[str, str], add: Callable[[str, str], None]
) -> None:
    """Evidence for a data candidate: the consumer stalled on a record it reports failing on (input_stalled), or
    only stalled (symptoms)."""
    for o in obs:
        if any(STALLED_RE.search(ln) and _token(consumer).search(ln) for ln in o.lines):
            # the record the consumer is stuck on (same observation) is the fact that matters most
            record = next((ln for ln in o.lines if "starting at the stalled offset" in ln), None)
            stalled = next(ln for ln in o.lines if STALLED_RE.search(ln) and _token(consumer).search(ln))
            offset = STALLED_RE.search(stalled).group(1)
            if consumer_reports_data_failure(notebook, consumer, offset):
                add("input_stalled", record or stalled)
            else:
                add("symptoms", stalled)
    if "input_stalled" not in kinds:
        repeated = repeated_input_failure(notebook, consumer)
        if repeated:
            add("input_stalled", repeated)


def _interaction_kinds(
    ctx: AgentContext, notebook: Notebook, pair: str, edges: list, add: Callable[[str, str], None]
) -> None:
    """Evidence for an interaction candidate: the caller's settings for the callee, and caller errors naming it."""
    caller, _, callee = pair.partition("--")
    cids = {_name(ctx, c): c for c in _comps(ctx)}
    a, b = cids.get(caller), cids.get(callee)
    settings = caller_settings_for(notebook, a, callee) if a else []
    if settings:
        add("configuration", f"{a} settings for {callee}: {', '.join(settings)}")
    if a and b and (a, b) in edges:
        add("pointed_at", f"ongoing errors of {a} name {b}")


def _object_kinds(ctx: AgentContext, key: str, parts: list[str], obs: list, add: Callable[[str, str], None]) -> None:
    """Evidence for a Service, ConfigMap, Secret or cluster-scoped object: lines naming it that show a change,
    configuration or an abnormal cluster finding, and its recent changes within the change window."""
    rx = _object_pattern(parts[0], parts[-1])
    for o in obs:
        for ln in o.lines:
            if not rx.search(ln):
                continue
            if _is_change(ln):
                add("change", ln)
            if CONFIG_RE.search(ln):
                add("configuration", ln)
            if o.tool == "cluster_objects" and (o.abnormal or 0.0) >= 0.5:
                add("cluster_finding", ln)
    for change in ((ctx.overview or {}).get("cluster") or {}).get("recent_changes") or []:
        if not isinstance(change, dict) or change.get("object") != key or not change_in_window(ctx, change):
            continue  # cluster history, or too long before the symptoms to have started them
        add("change", f"{key} changed {change.get('seconds_ago')}s ago, shortly before or since the symptoms began")


def candidate_evidence(
    ctx: AgentContext, notebook: Notebook, key: str, specific: set[str] | None = None
) -> tuple[list[str], list[str]]:
    """(evidence kinds in KINDS_ORDER, up to MAX_FACTS lines that show the strongest of them) for one candidate."""
    kinds: dict[str, str] = {}

    def add(kind: str, line: str) -> None:
        kinds.setdefault(kind, line)

    obs = notebook.observations
    edges = _call_edges(obs)
    parts = key.split("/")

    if key == "cluster_object":
        _cluster_object_kinds(obs, specific, add)
    elif len(parts) == 2 and key in _comps(ctx):  # a workload
        _workload_kinds(notebook, key, obs, edges, kinds, add)
    elif parts[0] == "data" and len(parts) == 3:
        _data_kinds(notebook, parts[2], obs, kinds, add)
    elif parts[0] == "interaction" and len(parts) == 3:
        _interaction_kinds(ctx, notebook, parts[2], edges, add)
    elif len(parts) in (2, 3):  # a Service, ConfigMap, Secret, or a cluster-scoped object (webhook, quota, ...)
        _object_kinds(ctx, key, parts, obs, add)
    ordered = [k for k in KINDS_ORDER if k in kinds]
    facts = [f"{k}: {_short(kinds[k])}" for k in ordered if k not in ("symptoms", "error_logs_only")][:MAX_FACTS]
    return ordered, facts


def caller_settings_for(notebook: Notebook, caller: str, callee_name: str) -> list[str]:
    """Env settings of `caller` (from its describe lines) whose names name the callee and say how it is called
    (timeouts, retries, limits), not merely where it is (address, host, port)."""
    want = [p for p in re.split(r"[-_.]", callee_name.upper()) if p]
    out: list[str] = []
    for o in notebook.observations:
        if o.target != caller or o.tool != "describe":
            continue
        for ln in o.lines:
            if not ln.startswith("env "):
                continue
            for pair in ln.split(": ", 1)[-1].split(", "):
                key, _, value = pair.partition("=")
                tokens = re.split(r"[_-]", key.upper())
                if want and all(w in tokens for w in want) and not _LOCATOR_SUFFIX.search(key) and value:
                    out.append(f"{key}={value}")
    return list(dict.fromkeys(out))[:6]


# An unchanged value in repeated error lines that identifies one message: offset=20, id: 42, seq 7, delivery_tag=3.
MESSAGE_ITEM_RE = re.compile(
    r"(?i)\b(offset|id|key|seq(uence)?|message|msg|record|delivery_?tag|tag|index|position)\s?[=:]?\s?[\w.-]+"
)
# Repeated failures that are about reaching or logging in to something, not about the data read.
NOT_INPUT_RE = re.compile(
    r"(?i)connect|refused|timed? ?out|timeout|unreachable|resolve|no route|reset by peer|unavailable|"
    r"auth|denied|forbidden|password|credential"
)
REPEAT_RE = re.compile(r"^repeats x\d+ over \d+s with unchanged values \[(.*?)\]: (.*)$")


def repeated_input_failure(notebook: Notebook, consumer: str) -> str | None:
    """A line of the consumer's logs showing it fail again and again on one message (any queue or broker): an error
    repeated with an unchanged message identifier, or a repeated failure to parse what it reads; not a connection
    or authentication failure. None when there is no such line."""
    for o in notebook.observations:
        if o.tool != "logs" or o.target.split("/")[-1] != consumer:
            continue
        for ln in o.lines:
            m = REPEAT_RE.match(ln)
            label = notebook.error_labels.get(ln)
            if not m or (label in ("connection", "authentication") if label else NOT_INPUT_RE.search(m.group(2))):
                continue
            values = [v.strip(" '\"") for v in m.group(1).split(",")]
            data_failure = label == "data" if label else bool(DATA_ERROR_RE.search(m.group(2)))
            if any(MESSAGE_ITEM_RE.fullmatch(v) for v in values) or data_failure:
                return ln
    return None


def consumer_reports_data_failure(notebook: Notebook, consumer: str, offset: str) -> bool:
    """Whether the consumer's own error lines (ongoing, or the last run's) name the offset it is stuck on or report
    a failure to process the data it reads."""
    names_offset = re.compile(rf"(?i)offset\W{{0,3}}{re.escape(offset)}(?!\d)")
    for o in notebook.observations:
        if o.tool != "logs" or o.target.split("/")[-1] != consumer:
            continue
        for ln in o.lines:
            label = notebook.error_labels.get(ln)
            data_failure = label == "data" if label else bool(DATA_ERROR_RE.search(ln))
            if (ln.startswith("ongoing") or ln.startswith("previous run")) and (
                names_offset.search(ln) or data_failure
            ):
                return True
    return False


def stalled_consumers(notebook: Notebook) -> list[str]:
    """Components a consumer-progress line reports stuck on one offset while their input grows."""
    out = []
    for o in notebook.observations:
        for ln in o.lines:
            m = STALLED_RE.search(ln) and STALLED_CONSUMER_RE.search(ln)
            if m:
                out.append(m.group(1))
    return list(dict.fromkeys(out))


def _login_failure(notebook: Notebook, line: str) -> bool:
    """Whether an error line reports a rejected login, by the decision model's label or, unlabelled, by its wording."""
    label = notebook.error_labels.get(line)
    return label == "authentication" if label else bool(LOGIN_FAILURE_RE.search(line))


def login_failures(notebook: Notebook) -> list[str]:
    """Components whose own ongoing log lines report failed logins or authentication."""
    out = []
    for o in notebook.observations:
        if o.tool == "logs" and any(ln.startswith("ongoing") and _login_failure(notebook, ln) for ln in o.lines):
            out.append(o.target)
    return list(dict.fromkeys(out))


def describe(base: str, kinds: list[str], facts: list[str]) -> str:
    """The origin option text: what the candidate is, its evidence kinds and the lines behind them."""
    text = f"{base} Evidence kinds: {', '.join(kinds) if kinds else 'none observed'}."
    if facts:
        text += " " + " | ".join(facts)
    return text


TRIAGE = (
    "Each candidate lists the evidence kinds the observations show for it. Decide in this order: "
    "0. stale_config (its running containers still hold values from before a write to the ConfigMap or Secret "
    "they read, and a failure goes with it: the write is the intended change, the component that did not pick it "
    "up is the origin); "
    "1. change (its own spec, Service, ConfigMap or Secret written shortly before the symptoms began, or its pods "
    "differing from its template) when that change explains the failures; "
    "2. configuration or cluster_finding (an abnormal setting of its own, or a webhook, quota, policy or DNS object "
    "acting on the failing pods); "
    "3. input_stalled (a consumer stuck on one record while later records arrive, and its own errors name that "
    "record's offset or a data-processing failure; or a queue consumer whose errors repeat on one and the same "
    "message: the data is the origin), "
    "pointed_at (other components' ongoing errors name it), own_failure_logs (its own authentication or "
    "data-processing failures); "
    "4. symptoms or error_logs_only never make a candidate the origin while another candidate has a kind from "
    "1-3. A component whose errors only name another component is that component's victim. Prefer a listed "
    "object over the generic cluster_object option when the finding is about that object."
)


def annotate(ctx: AgentContext, notebook: Notebook, criteria: dict[str, str], skip: set[str]) -> dict[str, str]:
    """criteria with each candidate's evidence kinds and facts appended (candidates in `skip` left as they are)."""
    out: dict[str, Any] = {}
    comps = _comps(ctx)
    specific = {
        k.split("/")[-1]
        for k in criteria
        if k.count("/") in (1, 2)
        and k not in comps
        and not k.startswith(("data/", "interaction/", "service/", "configmap/", "secret/"))
    }
    for key, text in criteria.items():
        if key in skip:
            out[key] = text
            continue
        kinds, facts = candidate_evidence(ctx, notebook, key, specific)
        out[key] = describe(text, kinds, facts)
    return out
