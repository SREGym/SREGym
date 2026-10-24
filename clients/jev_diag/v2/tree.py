"""Iterative, hypothesis-driven diagnosis: a decision tree where code owns the loop.

Node 1 (triage) ranks components over the compact snapshot, exactly as the
one-shot mode does. Code turns the distribution into a queue of hypotheses.

Node 2 (investigate) takes one component, fetches deep evidence for it
(investigate.collect_component_detail), and asks Jev narrow questions in one
request: is this the origin, a victim, unrelated, or undetermined; which linked
component to examine next; the cause category; the single key evidence item;
and whether one input item blocks the component.

Code decides the transition: a confirmed origin ends the search once the other
side of an authentication failure, or the owner of the fault object, has been
examined too; a victim verdict moves the named dependency to the front of the
queue; anything else moves on. A cluster node handles webhooks, quotas, DNS,
roles, and stuck objects. A step budget caps cost; the fallback prefers an
origin verdict, then the end of a strong victim chain, then triage.

Before the conclusion stands, it is tested against what it depends on: when
the concluded component still fails calling a dependency that is abnormal on
its own, that dependency is examined, and if it is an origin too, the two are
compared side by side for where the fault started.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Callable
from typing import Any

from typesafe_sdk import Choice, Noul

from clients.jev_diag.v2.changes import ONSET_TOLERANCE_SECONDS
from clients.jev_diag.v2.checks import describe_probe
from clients.jev_diag.v2.classifier import (
    CATEGORY_QUESTION,
    CLUSTER_LEVEL_KINDS,
    COMPONENT_QUESTION,
    EVIDENCE_QUESTION,
    FAULT_CATEGORIES,
    FAULT_KIND_QUESTION,
    OTHER_OPTION,
    ChoiceResult,
    Diagnosis,
    JevDiagnoser,
    build_component_questions,
    finding_linked,
    select_fault_object,
)
from clients.jev_diag.v2.collector import ClusterSnapshot, estimate_tokens, fit_state_to_budget
from clients.jev_diag.v2.deps import own_signals
from clients.jev_diag.v2.derive import diagnostic_signals
from clients.jev_diag.v2.report import build_report, changed_values, state_of

logger = logging.getLogger("all.jev_diag.v2.tree")

VERDICT_QUESTION = "verdict"
NEXT_QUESTION = "next_component"
CLUSTER_OBJECT_QUESTION = "cluster_object"
BLOCKED_QUESTION = "blocked_input"
TIEBREAK_QUESTION = "fault_start"
NONE_OPTION = "none"

DEFAULT_MAX_STEPS = 18
# An origin this sure ends the search once every hypothesis is examined. It also supplies independent support
# when opposite-order comparisons cannot agree; a weak origin probability alone cannot resolve that disagreement.
MIN_ORIGIN_PROBABILITY = 0.6
# Two candidates whose support differs by less than this are compared side by side: one examination's probability
# varies by about +-0.08 between identical asks, so a smaller difference is not a decision.
NEAR_TIE_MARGIN = 0.15
TOP_HYPOTHESES = 3  # triage's top candidates (cluster objects included) are all examined before any conclusion
MAX_SIDE_EVIDENCE = 8  # abnormal evidence items each side of a comparison shows
MIN_VICTIM_PROBABILITY = 0.5  # follow the dependency Jev names
MIN_NEXT_PROBABILITY = 0.3
MIN_QUEUE_PROBABILITY = 0.05  # every triage candidate at least this plausible is examined, budget permitting
MAX_QUEUE = 6
QUEUE_EXTENSION = 4  # extra candidates admitted on evidence alone
CLUSTER_KIND_MASS = 0.3  # triage probability on cluster-level fault kinds that queues the cluster node early
MIN_BLOCKED_PROBABILITY = 0.5
DECISIVE_TRIAGE_PROBABILITY = 0.9  # a first triage answer this sure is not asked again
MAX_NEXT_OPTIONS = 12
EVIDENCE_OPTION_CHARS = 200  # an option shows its item's text, so the choice is made on content
MAX_EVIDENCE = 32
DETAIL_TOKEN_BUDGET = 22000
# The kind of object that carries a fault of each cause category. Categories absent here are faults of the
# component's own spec or behavior and name no separate object.
CATEGORY_TO_OBJECT_KIND = {
    "rbac_permission": "rbac",
    "admission_or_namespace_policy": "admission_or_namespace",
    "service_routing": "service",
    "network_policy": "network_policy",
    "traffic_policy": "service",
    "dns_resolution": "dns",
    "credentials_or_auth": "config_object",
    "config_value": "config_object",
}
# Admission can rewrite these fields; when a rewritten field explains the category, the webhook carries the fault.
_ADMISSION_FIELDS = {
    "resource_limits": ("limits.", "requests."),
    "image_or_binary": (" image ",),
    "config_value": (" env adds ",),
}

VERDICTS: dict[str, dict[str, str]] = {
    "origin": {
        "what": "The application's failures (those in `incident`, and requests misrouted, dropped, or slowed without "
        "leaving a state) start at `candidate` itself or at a configuration object that applies to it: its own "
        "spec, environment, probes, resources, image, the content of a ConfigMap or "
        "Secret it reads, a Service, traffic policy, NetworkPolicy, quota, admission webhook, or DNS rule that "
        "targets it, its job definition, its permissions, or its own processing of its inputs (bad data, "
        "permission denials). This includes a client that still holds a value its own Secret or ConfigMap has "
        "since replaced: an environment variable keeps the value it had when the container started.",
        "not_for": "A component whose only problems are failed calls to another component; a value written into "
        "its spec by another workload, a scheduled job, or a controller (that writer is the origin); a server that "
        "rejects a credential a client still holds after the client's own Secret replaced it, when that source and "
        "working peers agree with the server and the client's failures match its stale value (that client is the "
        "origin); and trouble of its own that does not account for "
        "the failures in `incident`, such as a limit that merely looks small or one restart or OOM kill while the "
        "application's failures are elsewhere.",
    },
    "victim": {
        "what": "`candidate` misbehaves only because of another component: something it calls is failing or "
        "refusing it (its still-occurring errors name another component, a Service name, an address, or a backend "
        "role such as a database, cache, or queue), or another workload, scheduled job, or controller acts on it "
        "(writes its spec or objects, sends it load, preempts or displaces its pods). The other component may look "
        "healthy itself: a server whose password, ACL, or config changed shows the failure on its clients, unless "
        "those clients' own sources carry the change and a client still holds the old value.",
        "not_for": "A component whose own configuration or referenced config content changed or is abnormal in a "
        "way that explains its errors, even when those errors name a dependency.",
    },
    "unrelated": {
        "what": "`candidate` is healthy, or its abnormalities are stopped errors, settled deploy-time churn, or "
        "telemetry noise (trace, metric, or log export errors) with no link to the application's fault.",
        "not_for": "Any component with an unexplained abnormal signal of its own. A telemetry component whose own "
        "replicas are not ready, Pending, or crash looping is judged like any other component.",
    },
    "undetermined": {
        "what": "The evidence does not allow deciding between origin and victim; more of the application must "
        "be examined.",
        "not_for": "Cases where either origin or victim is clearly supported.",
    },
}

_ADDRESS_VALUE_RE = re.compile(r"(?i)(://|:\d{2,5}\b|\.svc\b|^[a-z0-9-]+$)")


def detail_evidence(comp: dict, detail: dict) -> list[dict]:
    """Evidence items for one investigated component, one fact per item, most specific first.

    Order: template changes and signals, code checks, probe results, spec settings, env values that changed,
    repeat, or hold addresses, probes, ConfigMap keys, RBAC, logs, events, dependencies, then the remaining env
    values. The list is capped, so the order decides what the model can select.
    """
    items: list[str] = []
    sources = {e["text"]: e for e in comp.get("change_evidence") or []}
    owner = {k: comp.get(k) for k in ("kind", "namespace", "name")}
    w = detail.get("workload") or {}
    changed_env: set[tuple[str, str]] = set()
    tc = detail.get("template_changes") or {}
    for change in tc.get("changes") or []:
        if "pod-template-hash" in change or "restartedAt" in change:
            continue  # bookkeeping of every rollout, not a setting
        items.append(f"pod template changed in revision {tc.get('revision')}: {change}")
        m = re.match(r"container (\S+) env (\S+) ", change)
        if m:
            changed_env.add((m.group(1), m.group(2)))
    for sig in comp.get("signals") or []:
        if "is terminating" not in sig:
            items.append(sig)
    items += comp.get("call_paths") or []
    items += detail.get("spec_checks") or []
    items += detail.get("service_checks") or []
    if detail.get("probe_text"):
        items.append(detail["probe_text"])
    for key in ("dns_policy", "dns_config", "host_aliases", "host_network", "node_selector", "tolerations", "strategy"):
        if w.get(key) not in (None, "ClusterFirst", False, [], {}):
            items.append(f"workload {key}: {w[key]}")
    if w.get("termination_grace_seconds") not in (None, 30):  # 30 s is the Kubernetes default
        items.append(f"pod terminationGracePeriodSeconds: {w['termination_grace_seconds']} (default 30)")
    salient_env: list[str] = []
    other_env: list[str] = []
    for c in (w.get("init_containers") or []) + (w.get("containers") or []):
        names = [e.get("name") for e in c.get("env") or []]
        for e in c.get("env") or []:
            value = e.get("value", e.get("from", "?"))
            text = f"container {c['name']} env {e['name']}={value}"
            if (
                (c["name"], e["name"]) in changed_env
                or names.count(e["name"]) > 1
                or (isinstance(value, str) and _ADDRESS_VALUE_RE.search(value) and not value.isdigit())
            ):
                salient_env.append(text)
            else:
                other_env.append(text)
        for kind, probe in (c.get("probes") or {}).items():
            items.append(f"container {c['name']} {kind} probe: {describe_probe(probe)}")
        if c.get("command"):
            items.append(f"container {c['name']} command: {' '.join(map(str, c['command']))[:500]}")
        if c.get("args"):
            items.append(f"container {c['name']} args: {' '.join(map(str, c['args']))[:500]}")
        items.append(f"container {c['name']} image {c.get('image')} resources {c.get('resources') or 'none'}")
    items += salient_env
    for name, cm in (detail.get("configmaps") or {}).items():
        for key, value in (cm.get("data") or {}).items():
            text = f"ConfigMap {name} key {key}: {value}"
            items.append(text)
            sources[text] = {"source_object": {"kind": "ConfigMap", "name": name, "namespace": comp.get("namespace")}}
    rbac = detail.get("rbac") or {}
    for role in rbac.get("roles") or []:
        first = len(items)
        items.append(
            f"RBAC {role.get('kind')} {role.get('name')} bound via {', '.join(role.get('bindings') or [])} grants: "
            f"{'; '.join(role.get('rules') or []) or 'nothing'}"
            + (f"; modified {role['modified']}" if role.get("changed") else "")
        )
        for sig in role.get("signals") or []:
            items.append(f"RBAC {role.get('kind')} {role.get('name')}: {sig}")
        for text in items[first:]:
            sources[text] = {
                "source_object": {
                    "kind": role.get("kind"),
                    "name": role.get("name"),
                    "namespace": role.get("namespace", comp.get("namespace")) if role.get("kind") == "Role" else None,
                }
            }
    if rbac.get("bindings") is not None:
        items.append(f"RBAC bindings for {rbac.get('service_account')}: {rbac.get('bindings') or 'none'}")
    log_items: list[str] = []
    event_items: list[str] = []
    dep_items: list[str] = []
    logs = detail.get("logs") or {}
    if logs.get("error_lines"):
        log_items.append(
            f"{logs.get('error_lines_still_occurring') or 0} error log lines still occurring, "
            f"{logs.get('error_lines_stopped') or 0} from errors that stopped"
        )
    for s in logs.get("error_samples", [])[:8]:
        state = s.get("state") or "undated"
        when = f", last seen {s['last_seen_seconds_ago']}s ago" if s.get("last_seen_seconds_ago") is not None else ""
        log_items.append(f"log x{s.get('count')} ({state}{when}): {s.get('line')}")
    for r in logs.get("repeated_failures") or []:
        log_items.append(
            f"repeated failure x{r['count']} over {r['span_seconds']}s with unchanged tokens "
            f"{', '.join(r['constant_tokens'])}: {r['line']}"
        )
    for key, lines in (logs.get("previous_container_logs") or {}).items():
        for ln in lines[-3:]:
            log_items.append(f"previous log {key}: {ln}")
    warnings = [e for e in detail.get("events") or [] if e.get("type") == "Warning"]
    for ev in warnings[:6]:  # filtered first, so Normal events never crowd out a Warning
        event_items.append(f"event {ev.get('reason')} x{ev.get('count')} on {ev.get('object')}: {ev.get('message')}")
    for dep in detail.get("failing_targets") or []:
        dep_items.append(f"this component's still-occurring error lines refer to its dependency {dep}")
    for cid, info in (detail.get("calls") or {}).items():
        if info.get("own_signals"):
            dep_items.append(f"dependency {cid} is abnormal on its own: {info['own_signals'][0]}")
        elif info.get("looks_healthy_on_its_own"):
            dep_items.append(f"dependency {cid} looks healthy on its own ({info.get('pods_ready')} pods ready)")
    dep_items += [f"reachability {line}" for line in detail.get("reachability") or []]
    for src, classes in (detail.get("called_by_with_errors") or {}).items():
        dep_items.append(f"still-occurring errors from {src} name this component: {classes}")
    telemetry = list(detail.get("telemetry") or [])
    dep_items += telemetry  # metrics after the dependency facts they support
    # Facts that describe normal state or plain measurements (a dependency that looks healthy, metric series, observed
    # calls) inform the verdict but cannot be the key evidence of a fault; a saturation fact can.
    context = {x for x in dep_items if x.startswith("dependency ") and "looks healthy" in x}
    context |= {x for x in telemetry if not x.startswith("saturation:")}
    return [
        {
            "id": f"E{i + 1}",
            "text": text,
            "source_object": sources.get(text, {}).get("source_object", owner),
            **({"context": True} if text in context or sources.get(text, {}).get("context") else {}),
        }
        for i, text in enumerate(cap_evidence(items, log_items, event_items, dep_items, other_env))
    ]


# Slots kept for each kind of evidence, so a long settings list never crowds out every log line or event.
EVIDENCE_RESERVE = {"logs": 4, "events": 3, "dependencies": 4}  # dependencies include telemetry facts


def cap_evidence(head: list[str], logs: list[str], events: list[str], deps: list[str], tail: list[str]) -> list[str]:
    """At most MAX_EVIDENCE unique items: the settings and checks first, each other kind keeping a few slots."""
    groups = {"logs": _unique(logs), "events": _unique(events), "dependencies": _unique(deps)}
    reserved = {k: min(n, len(groups[k])) for k, n in EVIDENCE_RESERVE.items()}
    head = [i for i in _unique(head) if not any(i in g for g in groups.values())]
    out = head[: MAX_EVIDENCE - sum(reserved.values())]
    for kind in ("logs", "events", "dependencies"):
        out += [i for i in groups[kind][: reserved[kind]] if i not in out]
    for extra in [*head, *(i for k in groups for i in groups[k]), *_unique(tail)]:
        if len(out) >= MAX_EVIDENCE:
            break
        if extra not in out:
            out.append(extra)
    return out[:MAX_EVIDENCE]


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def own_failure(comp: dict) -> bool:
    """A typed failure or configuration mismatch, excluding incidental export log lines and bare writes."""
    kinds = set(comp.get("evidence_kinds") or []) & {"symptoms", "own_failure_logs", "configuration"}
    return bool(kinds or state_of({**comp, "log_findings": [], "log_signals": []}))


def eligible(comp: dict, *, triage_first: bool = False) -> bool:
    """Own failures are investigated regardless of role; incidental export noise alone is not a hypothesis."""
    return bool(comp) and (comp.get("role") != "observability" or triage_first or own_failure(comp))


def failed_reference(investigated: dict, subjects: set[str]) -> dict | None:
    """One verified absent object on a subject's failed call, whether the subject is its client or dependency."""
    references = {}
    for result in investigated.values():
        for ref in result.get("missing_references") or []:
            if subjects & {ref["client"], ref["dependency"]}:
                references.setdefault((ref["kind"], ref["namespace"], ref["name"]), ref)
    return next(iter(references.values())) if len(references) == 1 else None


def linked_components(snapshot: ClusterSnapshot, cid: str, detail: dict) -> list[str]:
    """Components connected to `cid`: those it calls, those that call it or whose errors name it, and those that
    share a Service with it. Components with only incidental export noise are left out."""
    comp = snapshot.components[cid]
    linked = set(detail.get("calls") or {}) | set(detail.get("called_by_with_errors") or {})
    linked |= set(detail.get("failing_targets") or []) | set(comp.get("clients") or []) | set(comp.get("_calls") or [])
    for svc in comp.get("services") or []:
        linked |= set(svc.get("selects_multiple_workloads") or [])
    linked.discard(cid)
    return sorted(c for c in linked if eligible(snapshot.components.get(c) or {}))


def build_investigation_questions(
    snapshot: ClusterSnapshot, cid: str, evidence: list[dict], remaining: list[str], detail: dict | None = None
) -> dict[str, Choice | Noul]:
    comp = snapshot.components[cid]
    detail = detail or {}
    next_options = {
        other: f'{snapshot.components[other]["kind"]} `{snapshot.components[other]["name"]}` (`components["{other}"]`)'
        for other in remaining
    }
    next_options[NONE_OPTION] = (
        "No further component: `candidate` is the origin, or nothing in the state points elsewhere."
    )
    questions: dict[str, Choice | Noul] = {
        VERDICT_QUESTION: Choice(
            instructions={
                "question": f"Is `candidate` ({comp['kind']} `{comp['name']}`) the origin of the fault, a "
                "victim of another component, unrelated, or undetermined?",
                "how_to_decide": [
                    "`incident` lists the application's components that show a failure in their state and how each "
                    "fails. The origin is where the application's failures start: judge whether `candidate`'s own "
                    "state explains them, not only whether `candidate` has trouble of its own. `incident` is not "
                    "complete and not all of it is this fault: requests that a misconfigured proxy, Service, or "
                    "client sends to the wrong place, drops, or slows can leave no state on any component, and an "
                    "OOM kill, restart, or start-up error elsewhere may be unrelated background.",
                    "Decide in this order. First, `candidate.detail.spec_checks` and `candidate.detail.service_checks` "
                    "are mismatches code found inside this component's own spec or between its spec and the "
                    "Services it addresses (probe target versus container port, env address versus Service port, "
                    "Service targetPort or selector versus the workloads, a Local traffic policy versus where its "
                    "clients run). A mismatch that explains the component's symptom, or the failure of the requests "
                    "it serves or sends (which may show no state anywhere), makes it the `origin`, whatever its logs "
                    "say about dependencies.",
                    "Second, look for the mechanism in `candidate.detail`: `template_changes` (fields that changed "
                    "in the latest pod template revision), `admission_changes` (values in the running pod that its "
                    "template does not have), env values, probe targets, args, ConfigMap content, RBAC bindings, "
                    "dns/hostAliases settings, quota or webhook rejections in events, and error log classes.",
                    "Third, only errors that are still occurring count. Each error sample has `state`: `ongoing` or "
                    "`stopped` (silent for much longer than its usual gap: start-up retries and past episodes). "
                    "Stopped errors are not evidence for `victim` and do not name a next hop.",
                    "`candidate.detail.calls` lists components this one talks to (from env values and its error "
                    "logs), each judged from its own state: `looks_healthy_on_its_own`, `own_signals`, `pods_ready`, "
                    "`restarts`. `victim` needs still-occurring errors that name a dependency, or another workload, "
                    "job, or controller acting on `candidate`, and nothing abnormal in its own configuration that "
                    "explains those errors; the dependency may still look healthy (a server whose password, ACL, or "
                    "config changed fails its clients, not itself). `candidate.detail.called_by_with_errors` lists components whose errors name this one. "
                    "`candidate.detail.probe`, when present, is the result of a health command run inside this "
                    "component. `candidate.detail.reachability`, when present, says whether each dependency's name "
                    "resolves and its port accepts connections from inside `candidate`'s pod.",
                    "Pending, terminating, not-ready, or restarting pods are symptoms, not a verdict by themselves.",
                    "`telemetry_export_error_lines` and `telemetry_error_samples` are failures to export traces, "
                    "metrics, or logs to the telemetry pipeline. They do not break application requests and are "
                    "not evidence for origin, victim, or the next hop.",
                ],
            },
            criteria=VERDICTS,
        ),
        NEXT_QUESTION: Choice(
            instructions={
                "question": "If `candidate` is not confirmed as the origin, which component should be investigated next?",
                "how_to_decide": [
                    "First choice: the component that `candidate` fails to reach, authenticate with, or resolve, "
                    "as named by its still-occurring error messages. Map addresses, Service names, and backend roles "
                    "to the component in `candidate.detail.calls` that plays that role (`candidate.detail.failing_targets` "
                    "lists the matches code found). Choose it even if it shows no abnormal signals of its own.",
                    "Second choice: a component in `candidate.detail.called_by_with_errors`, `candidate.detail.clients`, "
                    "or `suspects` with abnormal signals of its own.",
                    f"Select `{NONE_OPTION}` when `candidate` is the origin.",
                ],
            },
            criteria=next_options,
        ),
        CATEGORY_QUESTION: Choice(
            instructions={
                "question": "If `candidate` is the origin, which category names the cause?",
                "how_to_decide": [
                    "Pods Pending, not ready, restarting, or terminating are symptoms; choose the category of what "
                    "made them so. Use `candidate.detail` and `evidence`.",
                    "Name the mechanism that produced the symptom (a probe, a rollout strategy, a volume, a Service, "
                    "a policy) before `config_value`, which is for a wrong value no more specific category describes.",
                    "A limit is the cause only if the limit is too low for the workload's normal needs; when a "
                    "recent change (see `template_changes`) makes the workload consume more, the change is the "
                    "cause. A value the running pod has but its template lacks was set at admission.",
                ],
            },
            criteria=FAULT_CATEGORIES,
        ),
        BLOCKED_QUESTION: Noul(
            instructions="Do `candidate.detail.logs` (error samples, `repeated_failures`, and the tail) show "
            "`candidate` failing again and again on one specific input item (a message, record, offset, row, file, "
            "or job), so that it cannot move on to later input?",
            criteria={
                "true": "The same input item fails repeatedly and later input waits behind it.",
                "false": "Failures vary, concern connections or configuration, or processing continues past them.",
            },
        ),
    }
    if any(not item.get("context") for item in evidence):
        questions[EVIDENCE_QUESTION] = Choice(
            instructions={
                "question": "Which single item in `evidence` most directly reveals the mechanism of the fault, "
                "assuming `candidate` is the origin?",
                "how_to_decide": [
                    "Prefer an item naming a configuration value, config content, policy, permission, or change over "
                    "an item describing a state (Pending, not ready, restarts).",
                    "Prefer an abnormal value (one that differs from the Kubernetes default, the previous revision, "
                    "or the component's peers) over an ordinary setting that happens to be listed.",
                    "Prefer an item with one specific value over an item listing many.",
                ],
            },
            criteria={
                item["id"]: f"Evidence item `{item['id']}`: {item['text'][:EVIDENCE_OPTION_CHARS]}"
                for item in evidence
                if not item.get("context")
            },
        )
    return questions


def build_cluster_questions(findings: list[dict]) -> dict[str, Choice]:
    options = {f"F{i + 1}": f"Finding `F{i + 1}` in `findings`." for i in range(len(findings))}
    options[NONE_OPTION] = "None of these objects explains the affected workloads."
    return {
        CLUSTER_OBJECT_QUESTION: Choice(
            instructions={
                "question": "Which cluster-level or namespace-level object in `findings` explains the abnormal "
                "signals of the `affected_components`?",
                "how_to_decide": [
                    "An object explains a workload's abnormal signals when what the object does (its rules, "
                    "selectors, targets, settings, or its own state) directly produces them; an object that merely "
                    "changed, or that the affected workloads do not depend on, does not.",
                    "`findings[].components` lists the workloads an object is known to act on. Objects that act on "
                    "every pod (network fabric, scheduling priority, nodes) need no such link.",
                    "Objects with the same `change_set` were written together in one operation, sometimes with a "
                    "workload's change: the one that acts (a job or controller that writes or patches other objects, "
                    "a webhook, a policy) is the cause, not the configuration or permission objects it uses. "
                    "`routine` findings are a controller's own bookkeeping and explain nothing by themselves.",
                ],
            },
            criteria=options,
        )
    }


def build_tiebreak_questions(ids: list[str]) -> dict[str, Choice]:
    """Where did the fault start, among examined hypotheses (each side shows its own examination's verdict)?"""
    criteria = {
        cid: f'The fault started at `sides["{cid}"]`; the other sides\' abnormal signals follow from it or are unrelated.'
        for cid in ids
    }
    return {
        TIEBREAK_QUESTION: Choice(
            instructions={
                "question": "Where did the fault start? `why_asked` says how the `sides` were chosen, and each "
                "side's `examination_verdict` is what its own examination concluded.",
                "how_to_decide": [
                    "For each side ask: if this side's change or abnormal setting were undone, would the other "
                    "sides' abnormal signals go away? The fault started at the side for which the answer is yes.",
                    "For a configuration mismatch, compare the source's relevant value and update time with each "
                    "client's read mode, container start, and evidence of which value it uses. An environment variable "
                    "keeps the value it had when the container started. When a client's own source now agrees with "
                    "the server and working peers, but that client still uses the previous value and its failures "
                    "match the mismatch, the stale client is the origin. A source write time alone does not prove "
                    "its relevant value changed. Adoption alone does not establish correctness: a new value already "
                    "adopted by several components may itself be faulty. Do not clear a server merely because peers "
                    "adopted its change.",
                    "A side that acts on another (writes its spec or objects, admits or rewrites it, schedules or "
                    "preempts it, sends it load) started the fault when that action explains the other side's "
                    "state, even though the abnormal value or state shows on the other side.",
                    "A side that only fails calling another side (`links`: fails calling) did not start the fault, "
                    "unless its own configuration is what makes those calls fail; a side merely configured to call "
                    "another is not thereby its victim. `links` lists every connection code found.",
                ],
            },
            criteria=criteria,
        )
    }


def shows_own_state(snapshot: ClusterSnapshot, cid: str, result: dict) -> bool:
    """Whether a dependency a victim points at can be where the fault started: its own examination did not find it
    unrelated, or it shows something of its own (a signal such as rejected logins, a pod template change, a recorded
    change, a probe finding). A dependency judged unrelated that shows nothing changed in it that could fail its
    clients."""
    if result.get("verdict") != "unrelated":
        return True
    comp = snapshot.components.get(cid) or {}
    legacy_change = comp.get("most_recent_change") and not comp.get("change_evidence")
    return bool(diagnostic_signals(comp) or changed_values(comp) or legacy_change or result.get("probe"))


def _change_time(comp: dict):
    from clients.jev_diag.timeutil import parse_time

    return parse_time((comp.get("template_changes") or {}).get("changed_at") or comp.get("modified"))


def link_facts(snapshot: ClusterSnapshot, a: str, b: str, investigated: dict | None = None) -> list[str]:
    """Every connection code can see between two components (each taken with the workloads that got the same change
    as it): error lines naming the other, logins one rejects from the other, addresses one calls, examined calls
    and failing targets, timing-out call paths, shared config objects, and changes made within the same seconds."""
    from clients.jev_diag.timeutil import seconds_between
    from clients.jev_diag.v2.report import SAME_CHANGE_SECONDS, same_change

    investigated = investigated or {}
    side_a, side_b = {a, *same_change(snapshot, a)}, {b, *same_change(snapshot, b)}
    out = []
    if side_a & side_b:
        out.append("the same pod template change was applied to both")
    for xs, ys in ((side_a, side_b), (side_b, side_a)):
        for x in sorted(xs):
            cx, r = snapshot.components.get(x) or {}, investigated.get(x) or {}
            for y in sorted(ys):
                if y in (cx.get("errors_point_to") or []):
                    out.append(f"{x}'s still-occurring error lines name {y}")
                if y in (cx.get("login_candidates") or []):
                    out.append(f"{x} rejects logins that {y} may present")
                if y in (r.get("failing_targets") or []):
                    out.append(f"{x} fails calling {y}: its still-occurring errors refer to it")
                elif y in (cx.get("_calls") or []) or y in (r.get("calls") or []):
                    out.append(f"{x} is configured to call {y}")
    shared = set((snapshot.components[a]).get("config_objects") or {}) & set(
        (snapshot.components[b]).get("config_objects") or {}
    )
    out += [f"both use {obj}" for obj in sorted(shared)]
    for path in snapshot.cluster.get("call_paths") or []:
        if {path["caller"], path["callee"]} & side_a and {path["caller"], path["callee"]} & side_b:
            out.append(f"calls from {path['caller']} to {path['callee']} time out")
    ta, tb = _change_time(snapshot.components[a]), _change_time(snapshot.components[b])
    if ta and tb and abs(seconds_between(ta, tb)) <= SAME_CHANGE_SECONDS:
        out.append("both were changed within the same few seconds")
    return list(dict.fromkeys(out))


def explained_by(snapshot: ClusterSnapshot, cid: str | None, fault_object: dict | None) -> set[str]:
    """Components whose failures one cause accounts for: the cause, the same change on other workloads, the users of
    its fault object, and, transitively, every component whose errors name an explained one, that calls one, or that
    rejects logins from one."""
    from clients.jev_diag.v2.report import same_change

    out = set((fault_object or {}).get("components") or [])
    if cid in snapshot.components:
        out |= {cid, *same_change(snapshot, cid)}
    grew = True
    while grew:
        grew = False
        for other, comp in snapshot.components.items():
            reaches = set(comp.get("errors_point_to") or []) | set(comp.get("_calls") or [])
            reaches |= set(comp.get("login_candidates") or [])
            if other not in out and reaches & out:
                out.add(other)
                grew = True
    return out


def platform_change_first(snapshot: ClusterSnapshot) -> bool:
    """Whether the change nearest before the symptoms (changes.rank_changes) is an object outside the application's
    workloads that acts on none of them in particular (linked to no workload, or to most of them at once), so the
    cluster-object path is examined whatever triage weighted it: a change just before an incident is the first
    suspect, however unremarkable its object looks, and no single workload hypothesis stands for it."""
    changes = snapshot.cluster.get("recent_changes") or []
    first = changes[0] if changes else None
    if not first or not snapshot.cluster.get("findings"):
        return False
    before = first.get("seconds_before_symptoms")
    near = before is None or before >= -ONSET_TOLERANCE_SECONDS
    affects = first.get("affects") or []
    broad = not affects or len(affects) >= max(2, len(snapshot.components) // 2)
    return near and first.get("object") not in snapshot.components and broad


MAX_TOGETHER = 2  # workloads admitted because they were written in one operation with a hypothesis


def written_together(snapshot: ClusterSnapshot, hypotheses: list[str]) -> list[str]:
    """Candidates written in one operation (a change set: same writer, within seconds) with a hypothesis's own
    change. The cluster node, when an outside object with a finding was among them: a value written into a workload
    alongside an automation object, a role, or a policy may be that object's doing. Other workloads of the set, at
    most MAX_TOGETHER: one operation may have broken one of them and touched the others."""
    sets = {
        ch["object"]: ch.get("change_set")
        for ch in snapshot.cluster.get("recent_changes") or []
        if ch.get("change_set") and ch.get("object") in hypotheses
    }
    if not sets:
        return []
    out: list[str] = []
    in_sets = set(sets.values())
    if any(f.get("change_set") in in_sets for f in snapshot.cluster.get("findings") or []):
        out.append(OTHER_OPTION)
    others = [
        ch["object"]
        for ch in snapshot.cluster.get("recent_changes") or []
        if ch.get("change_set") in in_sets
        and ch.get("object") in snapshot.components
        and ch["object"] not in hypotheses
        and eligible(snapshot.components[ch["object"]])
    ]
    return out + others[:MAX_TOGETHER]


MAX_INCIDENT = 12  # failing components listed for an examination


def incident(snapshot: ClusterSnapshot) -> list[dict]:
    """The application's failing components and how each fails, from their typed state (report.state_of): what an
    origin has to explain. A telemetry component counts by its own state (replicas, pods, restarts), not by its
    log lines, which are mostly export noise; components with more symptoms come first."""
    rows = []
    for cid, comp in snapshot.components.items():
        if comp.get("role") == "observability":
            comp = {**comp, "log_findings": [], "log_signals": []}
        symptoms = state_of(comp)
        if symptoms:
            rows.append({"component": cid, "symptoms": symptoms})
    return sorted(rows, key=lambda r: -r["symptoms"].count(","))[:MAX_INCIDENT]


class IterativeDiagnoser(JevDiagnoser):
    """Decision-tree diagnosis on top of the one-shot triage."""

    def __init__(
        self,
        client=None,
        *,
        fetch_detail: Callable[[ClusterSnapshot, str], dict],
        max_steps: int = DEFAULT_MAX_STEPS,
        probe: Callable[[ClusterSnapshot, str, str | None], dict | None] | None = None,
        detail_token_budget: int = DETAIL_TOKEN_BUDGET,
        **kwargs: Any,
    ) -> None:
        super().__init__(client, **kwargs)
        self.fetch_detail = fetch_detail
        self.max_steps = max_steps
        self.probe = probe
        self.detail_token_budget = detail_token_budget
        self._probe_cache: dict[tuple[str, str], dict | None] = {}

    # ------------------------------------------------------------------ nodes

    def triage(self, snapshot: ClusterSnapshot) -> tuple[ChoiceResult, ChoiceResult | None]:
        full_state = snapshot.to_state()
        state, trims = fit_state_to_budget(full_state, self.state_token_budget)
        self._trace(
            "jev",
            "budget_fit",
            label="triage",
            budget=self.state_token_budget,
            tokens_before=estimate_tokens(full_state),
            tokens_after=estimate_tokens(state),
            applied=trims,
        )
        # Asked twice, the second time with the options in reverse order; the two answers are averaged, and when
        # their picks differ both are examined before an origin is concluded (see _diagnose).
        questions = build_component_questions(snapshot)
        # if a model limits the options per question, the components showing problems are offered first
        self.option_priority = sorted(
            snapshot.components,
            key=lambda c: (
                not snapshot.components[c].get("evidence_kinds"),
                -len(snapshot.components[c].get("signals") or []),
            ),
        )
        answers = [self._ask("triage", state, questions)]
        first = answers[0].choices[COMPONENT_QUESTION]
        if (first.probabilities or {}).get(first.choice, 0.0) < DECISIVE_TRIAGE_PROBABILITY:
            answers.append(self._ask("triage:reversed", state, reversed_options(questions)))
        picks = [a.choices[COMPONENT_QUESTION].choice for a in answers]
        self.triage_picks = list(dict.fromkeys(picks))
        if len(self.triage_picks) == 2:
            from clients.jev_diag.v2.report import same_change

            if self.triage_picks[1] in same_change(snapshot, self.triage_picks[0]):
                self.triage_picks = self.triage_picks[:1]  # the same change on both: the asks agree
        component = averaged([ChoiceResult.from_answer(a.choices[COMPONENT_QUESTION]) for a in answers])
        kind = (
            averaged([ChoiceResult.from_answer(a.choices[FAULT_KIND_QUESTION]) for a in answers])
            if all(FAULT_KIND_QUESTION in a.choices for a in answers)
            else None
        )
        self._trace("decide", "triage_agreement", picks=picks, agreed=len(self.triage_picks) == 1)
        return component, kind

    def initial_queue(
        self, snapshot: ClusterSnapshot, component: ChoiceResult, kind: ChoiceResult | None = None
    ) -> list[str]:
        """Plausible triage candidates in triage order, with two adjustments.

        A telemetry component with only a write or export error is eligible only when triage put it first; an own
        typed failure is eligible at any rank and can extend the shortlist. When triage puts at least
        CLUSTER_KIND_MASS of its fault-kind probability on cluster-level objects, the cluster node is examined
        right after the first candidate.
        """
        findings = snapshot.cluster.get("findings")
        shortlist: list[str] = []
        for cid, p in component.ranked():
            if len(shortlist) >= MAX_QUEUE:
                break
            if cid == OTHER_OPTION:
                if not findings:
                    continue
            elif not eligible(snapshot.components.get(cid) or {}, triage_first=cid == component.choice):
                continue
            if p < MIN_QUEUE_PROBABILITY and shortlist:
                break
            shortlist.append(cid)
        # Components with application-level evidence that triage left out, strongest evidence kind first:
        # a configuration defect or another component's errors naming it outrank a bare change.
        priority = {"configuration": 0, "pointed_at": 1, "own_failure_logs": 2, "change": 3, "symptoms": 4}
        extra = []
        for cid, comp in snapshot.components.items():
            kinds = set(comp.get("evidence_kinds") or [])
            if cid not in shortlist and eligible(comp) and (kinds & set(priority) or own_failure(comp)):
                rank = min((priority[k] for k in kinds if k in priority), default=priority["symptoms"])
                extra.append((rank, -component.probabilities.get(cid, 0.0), cid))
        for _, _, cid in sorted(extra)[: max(0, MAX_QUEUE + QUEUE_EXTENSION - len(shortlist))]:
            shortlist.append(cid)

        def demote(cid: str) -> bool:
            return not eligible(snapshot.components.get(cid) or {})

        demoted = [c for c in shortlist if demote(c)]
        queue = [c for c in shortlist if c not in demoted] + demoted
        mass = sum(p for k, p in (kind.probabilities if kind else {}).items() if k in CLUSTER_LEVEL_KINDS)
        if findings and mass >= CLUSTER_KIND_MASS:
            if OTHER_OPTION in queue:
                queue.remove(OTHER_OPTION)
            queue.insert(1 if queue else 0, OTHER_OPTION)
        return queue

    def investigate(
        self, snapshot: ClusterSnapshot, cid: str, triage: ChoiceResult, investigated: dict, remaining: list[str]
    ) -> dict:
        comp = snapshot.components[cid]
        detail = self.fetch_detail(snapshot, cid)
        self._maybe_probe(snapshot, cid, detail, investigated)
        evidence = detail_evidence(comp, detail)
        linked = [c for c in linked_components(snapshot, cid, detail) if c not in investigated]
        options = linked[:MAX_NEXT_OPTIONS]
        if len(options) < 2:
            for other, _ in triage.ranked():
                if len(options) >= 4:
                    break
                if other in remaining and other not in options and eligible(snapshot.components[other]):
                    options.append(other)
        state = {
            "application": snapshot.app,
            "candidate": {
                "id": cid,
                "kind": comp["kind"],
                "name": comp["name"],
                "namespace": comp["namespace"],
                "signals": comp.get("signals"),
                "evidence_kinds": comp.get("evidence_kinds"),
                "role": comp.get("role"),
                "detail": detail,
            },
            "evidence": evidence,
            "incident": incident(snapshot),
            # triage's leading suspects as a set: neither its probabilities nor its order, which varies between
            # identical asks, should anchor this candidate's own verdict
            "suspects": sorted(c for c, _ in triage.ranked()[:8]),
            # earlier verdicts without their probabilities, so a sure-sounding earlier answer does not anchor this one
            "investigated_so_far": [{"component": c, "verdict": r["verdict"]} for c, r in investigated.items()],
            "cluster_findings": snapshot.cluster.get("findings") or [],
            "components": {},
        }
        fitted, trims = fit_state_to_budget(state, self.detail_token_budget)
        fitted.pop("components", None)
        self._trace(
            "jev",
            "budget_fit",
            label=f"investigate:{cid}",
            budget=self.detail_token_budget,
            tokens_before=estimate_tokens(state),
            tokens_after=estimate_tokens(fitted),
            applied=trims,
        )
        response = self._ask(
            f"investigate:{cid}",
            fitted,
            build_investigation_questions(
                snapshot, cid, fitted.get("evidence") or [], options, fitted.get("candidate", {}).get("detail", {})
            ),
        )
        verdict = ChoiceResult.from_answer(response.choices[VERDICT_QUESTION])
        nxt = ChoiceResult.from_answer(response.choices[NEXT_QUESTION])
        category = ChoiceResult.from_answer(response.choices[CATEGORY_QUESTION])
        key = (
            ChoiceResult.from_answer(response.choices[EVIDENCE_QUESTION])
            if EVIDENCE_QUESTION in response.choices
            else None
        )
        blocked_p = response.nouls[BLOCKED_QUESTION].noul if BLOCKED_QUESTION in response.nouls else None
        key_text = next((e["text"] for e in evidence if key and e["id"] == key.choice), None)
        blocked = None
        if blocked_p is not None and blocked_p >= MIN_BLOCKED_PROBABILITY:
            repeated = (detail.get("logs") or {}).get("repeated_failures") or []
            blocked = {
                "source": "logs",
                "evidence": repeated[0]["line"] if repeated else key_text,
                "probability": round(float(blocked_p), 4),
            }
        return {
            "component": cid,
            "calls": list((detail.get("calls") or {}).keys()),
            "failing_targets": list(detail.get("failing_targets") or []),
            "spec_checks": list(detail.get("spec_checks") or []) + list(detail.get("service_checks") or []),
            "verdict": verdict.choice,
            "verdict_probabilities": verdict.probabilities,
            "origin_p": verdict.probabilities.get("origin", 0.0),
            "victim_p": verdict.probabilities.get("victim", 0.0),
            "next": nxt.choice,
            "next_p": nxt.probabilities.get(nxt.choice, 0.0),
            "next_ranked": nxt.ranked()[:3],
            "category": category,
            "key_evidence": key,
            "blocked_input": blocked,
            "probe": detail.get("probe"),
            "missing_references": detail.get("missing_references") or [],
            "evidence": evidence,
            "detail_tokens": estimate_tokens(fitted),
        }

    def _probe_clients(self, snapshot: ClusterSnapshot, cid: str, investigated: dict) -> list[str]:
        """Clients with active failed calls to a backend, from collection or later investigation."""
        if self.probe is None:
            return []
        clients = {
            c
            for c, r in investigated.items()
            if r["victim_p"] >= MIN_VICTIM_PROBABILITY and cid in (r.get("failing_targets") or [])
        }
        observed = {c for c, comp in snapshot.components.items() if cid in (comp.get("errors_point_to") or [])}
        comp = snapshot.components[cid]
        pods = comp.get("pods") or []
        healthy = (
            not comp.get("pods_not_verified")
            and bool(pods)
            and all(
                p.get("phase") == "Running"
                and p.get("ready")
                and p["ready"].split("/")[0] == p["ready"].split("/")[1] != "0"
                for p in pods
            )
        )
        if own_signals(comp) or not healthy:
            return []
        return sorted(clients - {cid}) + sorted(observed - clients - {cid})

    def _has_new_probe(self, snapshot: ClusterSnapshot, cid: str, investigated: dict) -> bool:
        return any(
            (cid, client) not in self._probe_cache for client in self._probe_clients(snapshot, cid, investigated)
        )

    def _maybe_probe(self, snapshot: ClusterSnapshot, cid: str, detail: dict, investigated: dict) -> None:
        """Probe each relevant client/backend pair once, reusing observations on re-examination."""
        clients = self._probe_clients(snapshot, cid, investigated)
        if not clients:
            return
        client = next((c for c in clients if (cid, c) not in self._probe_cache), clients[0])
        key = (cid, client)
        if key not in self._probe_cache:
            try:
                result = self.probe(snapshot, cid, client)
            except Exception as exc:  # noqa: BLE001 - a probe never stops the diagnosis
                result = None
                self._trace("collect", "probe", component=cid, client=client, error=f"{type(exc).__name__}: {exc}")
            self._probe_cache[key] = result
            self._trace("collect", "probe", component=cid, client=client, result=result)
        from clients.jev_diag.v2.probes import agrees_with_client, describe_probe_result

        findings = [
            result
            for (backend, _), result in self._probe_cache.items()
            if backend == cid and result and not agrees_with_client(result)
        ]
        if findings:  # a healthy or unavailable later pair does not erase an earlier failed pair
            detail["probe"] = findings[0]
            detail["probe_text"] = "\n".join(dict.fromkeys(describe_probe_result(cid, r) for r in findings))

    def cluster_node(self, snapshot: ClusterSnapshot) -> dict | None:
        findings = snapshot.cluster.get("findings") or []
        if not findings:
            return None
        affected = {
            cid: {"signals": c.get("signals"), "warning_events": (c.get("warning_events") or [])[:3]}
            for cid, c in snapshot.components.items()
            if c.get("signals") and eligible(c)
        }
        state = {
            "application": snapshot.app,
            "findings": [{"id": f"F{i + 1}", **f} for i, f in enumerate(findings)],
            "affected_components": affected,
            "cluster_dns": snapshot.cluster.get("cluster_dns"),
        }
        fitted, _ = fit_state_to_budget({**state, "components": {}}, self.detail_token_budget)
        fitted.pop("components", None)
        response = self._ask("cluster_objects", fitted, build_cluster_questions(fitted.get("findings") or []))
        result = ChoiceResult.from_answer(response.choices[CLUSTER_OBJECT_QUESTION])
        chosen = None
        if result.choice != NONE_OPTION:
            chosen = findings[int(result.choice[1:]) - 1]
        return {"result": result, "object": chosen}

    @staticmethod
    def side(snapshot: ClusterSnapshot, cid: str, investigated: dict, cluster_pick: dict | None = None) -> dict:
        """What a comparison shows of one hypothesis: its own facts, never a probability (which would anchor it)."""
        if cid == OTHER_OPTION:
            fo = cluster_pick or {}
            return {
                "component": f"{fo.get('kind')} {fo.get('name')}",
                "kind": fo.get("kind"),
                "namespace": fo.get("namespace"),
                "finding": fo.get("signal"),
                "acts_on": fo.get("components") or [],
            }
        r = investigated[cid]
        comp = snapshot.components[cid]
        key = next(
            (e["text"] for e in r["evidence"] if r["key_evidence"] and e["id"] == r["key_evidence"].choice), None
        )
        return {
            "component": cid,
            "kind": comp["kind"],
            "examination_verdict": r["verdict"],
            "cause_category": r["category"].choice,
            "key_evidence": key,
            "code_checks": r["spec_checks"],
            "abnormal_evidence": [e["text"] for e in r["evidence"] if not e.get("context")][:MAX_SIDE_EVIDENCE],
            "signals": (comp.get("signals") or [])[:12],
            "config_objects": comp.get("config_objects"),
            "configuration_adoption": [
                {
                    "source": source,
                    "updated_at": at,
                    "read_mode": "fixed at container start",
                    "observation": "at least one running container started before this source update; check whether its relevant value changed",
                }
                for source, at in (comp.get("_holds_previous") or {}).items()
            ],
        }

    def compare_origins(
        self,
        snapshot: ClusterSnapshot,
        hypotheses: dict[str, float],
        investigated: dict,
        reason: str,
        *,
        cluster_pick: dict | None = None,
    ) -> str | None:
        """Both orders must agree. Disagreement contributes no origin evidence of its own."""
        ids = sorted(hypotheses, key=lambda c: -hypotheses[c])
        workloads = [c for c in ids if c != OTHER_OPTION]
        links: list[str] = []
        for i, a in enumerate(workloads):
            for b in workloads[i + 1 :]:
                links += link_facts(snapshot, a, b, investigated)
        if OTHER_OPTION in hypotheses and cluster_pick:
            fo = f"{cluster_pick.get('kind')} {cluster_pick.get('name')}"
            links += [f"{fo} acts on {c}" for c in workloads if c in (cluster_pick.get("components") or [])]
        sides = {c: self.side(snapshot, c, investigated, cluster_pick) for c in ids}
        answers = []
        for order in (ids, ids[::-1]):
            state = {
                "application": snapshot.app,
                "why_asked": reason,
                "links": links or ["none found"],
                "sides": {c: sides[c] for c in order},
            }
            response = self._ask(f"tiebreak:{'|'.join(order)}", state, build_tiebreak_questions(order))
            answers.append(ChoiceResult.from_answer(response.choices[TIEBREAK_QUESTION]))
        support = {c: sum(a.probabilities.get(c, 0.0) for a in answers) / len(answers) for c in ids}
        ranked = sorted(support.items(), key=lambda kv: -kv[1])  # stable: a tie keeps the examinations' order
        agreed = len({a.choice for a in answers}) == 1
        winner = ranked[0][0] if agreed and ranked[0][1] > ranked[1][1] else None
        self._trace(
            "decide", "tiebreak", sides=ids, reason=reason, links=links, ranked=ranked, agreed=agreed, winner=winner
        )
        return winner

    def other_side(self, snapshot: ClusterSnapshot, cid: str, result: dict) -> list[tuple[str, str]]:
        """Components to examine before concluding `cid`, in order, each with the reason (at most two).

        Two cases: for a credential or permission cause, the object that carries it belongs to another component,
        not to `cid`; or the cause is authentication, which always involves a client and a server. A server that
        rejects logins has ranked candidate clients (derive.server_login_rejections); the top two are examined.
        """
        category = result["category"].choice
        kind = CATEGORY_TO_OBJECT_KIND.get(category) if category in ("credentials_or_auth", "rbac_permission") else None
        if kind:
            fo = select_fault_object(
                snapshot, cid, ChoiceResult(choice=kind, confidence=1.0, probabilities={kind: 1.0})
            )
            if fo and not finding_linked(snapshot, fo, cid):
                users = [c for c in snapshot.components if c != cid and finding_linked(snapshot, fo, c)]
                if users:
                    return [
                        (users[0], f"The fault object {fo['kind']} {fo['name']} belongs to {users[0]}, not to {cid}")
                    ]
        if category == "credentials_or_auth":
            comp = snapshot.components[cid]
            if comp.get("login_candidates"):
                return [
                    (client, f"{cid} rejects logins; the rejected credentials come from its client {client}")
                    for client in comp["login_candidates"][:2]
                ]
            targets = result.get("failing_targets") or comp.get("errors_point_to") or []
            if targets:
                return [(targets[0], f"Authentication fails between {cid} and the server it calls, {targets[0]}")]
        return []

    # ------------------------------------------------------------------ loop

    def diagnose(self, snapshot: ClusterSnapshot) -> Diagnosis:
        """The decision tree; when the decision model cannot be reached (credit, outage), a report from code alone."""
        if not snapshot.components:
            raise ValueError("No workloads found in the application namespaces; nothing to classify.")
        self._probe_cache.clear()
        self._progress: dict = {}
        try:
            return self._diagnose(snapshot)
        except Exception as exc:  # noqa: BLE001 - a diagnosis without the model beats submitting nothing
            reason = f"{type(exc).__name__}: {exc}"
            logger.error("decision model unavailable, reporting from what is known: %s", reason)
            self._trace("decide", "fallback_without_model", error=reason, traceback=traceback.format_exc())
            try:
                return self._from_progress(snapshot, reason)
            except Exception:  # noqa: BLE001
                return code_only_diagnosis(snapshot, reason)

    def _from_progress(self, snapshot: ClusterSnapshot, reason: str) -> Diagnosis:
        """The conclusion the work done so far supports when a later model request fails: the candidate with the most
        support, without comparisons; without examinations, triage; without a triage, code alone."""
        p = self._progress
        component, kind = p.get("component"), p.get("kind")
        if component is None:
            return code_only_diagnosis(snapshot, reason)
        investigated = p["investigated"]
        support = self.support(snapshot, component, investigated)
        if p.get("cluster_lead") is not None:
            support[OTHER_OPTION] = (p.get("cluster_p") or 0.0, None)
        if support:
            concluded = max(support, key=lambda c: support[c][0])
            why = support[concluded][1] or "most support among examined candidates"
        else:
            concluded, why = self.fallback(snapshot, component)
        cluster_pick = p.get("cluster_lead") if concluded == OTHER_OPTION else None
        info = self._characterize(snapshot, concluded, investigated, kind, cluster_pick, why)
        final = ChoiceResult(
            choice=concluded, confidence=component.confidence, probabilities=dict(component.probabilities)
        )
        diagnosis = self._diagnosis(snapshot, concluded, final, info, kind)
        diagnosis.investigation = {
            "steps": p["steps"],
            "concluded": concluded,
            "fallback": f"{why}; decision model failed later: {reason}",
        }
        diagnosis.text = build_report(snapshot, diagnosis)
        return diagnosis

    def _diagnose(self, snapshot: ClusterSnapshot) -> Diagnosis:
        component, kind = self.triage(snapshot)
        queue = self.initial_queue(snapshot, component, kind)
        if OTHER_OPTION in getattr(self, "triage_picks", []) and snapshot.cluster.get("findings"):
            # one of the two asks put the fault on a cluster-level object: that is checked right after the top pick
            if OTHER_OPTION in queue:
                queue.remove(OTHER_OPTION)
            queue.insert(1 if queue else 0, OTHER_OPTION)
        self._trace(
            "decide",
            "component",
            chosen=component.choice,
            confidence=component.confidence,
            ranked=component.ranked()[:8],
            fault_kind=kind.choice if kind else None,
            queue=list(queue),
            mode="tree",
        )

        investigated: dict[str, dict] = {}
        steps: list[dict] = []
        concluded: str | None = None
        confirmed: dict[str, float] = {}  # origin probability of every hypothesis judged an origin
        cluster_pick: dict | None = None
        cluster_lead: dict | None = None  # the cluster question's first choice, even below MIN_ORIGIN_PROBABILITY
        # Triage's top candidates (and the other ask's pick when the two asks disagree) are hypotheses: every one is
        # examined before anything is concluded, so a confirmed first pick never hides a better second.
        ranked = component.ranked()

        def admissible(c: str, rank: int) -> bool:
            if c == OTHER_OPTION:
                return bool(snapshot.cluster.get("findings"))
            return eligible(snapshot.components.get(c) or {}, triage_first=rank == 0)

        # triage's top candidates: triage is often sure of a wrong first pick while the true cause ranks just below
        admitted = [
            c
            for rank, (c, p) in enumerate(ranked[:TOP_HYPOTHESES])
            if p >= MIN_QUEUE_PROBABILITY and admissible(c, rank)
        ]
        picks = [c for c in getattr(self, "triage_picks", []) if admissible(c, 0) and c != OTHER_OPTION]
        own_failures = [
            c
            for c in queue
            if c in snapshot.components and own_failure(snapshot.components[c]) and eligible(snapshot.components[c])
        ]
        hypotheses = list(dict.fromkeys([*admitted, *picks, *own_failures]))
        if OTHER_OPTION not in hypotheses and platform_change_first(snapshot):
            hypotheses.append(OTHER_OPTION)  # the change nearest the symptoms is outside the workloads: a suspect
        if OTHER_OPTION not in hypotheses and any(f.get("platform") for f in snapshot.cluster.get("findings") or []):
            hypotheses.append(OTHER_OPTION)  # several workloads fail in one platform layer: the platform is a suspect
        for c in written_together(snapshot, [h for h in hypotheses if h in snapshot.components]):
            if c not in hypotheses:
                hypotheses.append(c)  # written in one operation with a hypothesis's own change: the actor may be it
        for c in hypotheses:
            if c not in queue:
                queue.append(c)
        examined: set[str] = set()
        checked_other_side: set[str] = set()
        cluster_p = 0.0
        self._progress = {"component": component, "kind": kind, "investigated": investigated, "steps": steps}
        while queue and len(steps) < self.max_steps:
            pending = [h for h in hypotheses if h not in examined]
            if (confirmed or cluster_pick) and not pending:
                break  # every hypothesis is examined and at least one is an origin: compare them below
            if confirmed or cluster_pick:  # an origin is known: the remaining hypotheses come before any other hop
                queue = pending + [c for c in queue if c not in pending]
            cid = queue.pop(0)
            examined.add(cid)
            if cid == OTHER_OPTION:
                node = self.cluster_node(snapshot)
                steps.append(
                    {"step": len(steps) + 1, "node": "cluster", "result": node["result"].ranked()[:3] if node else None}
                )
                self._trace("decide", "step", **steps[-1])
                cluster_p = node["result"].probabilities.get(node["result"].choice, 0) if node else 0.0
                cluster_lead = node["object"] if node else None
                self._progress.update(cluster_lead=cluster_lead, cluster_p=cluster_p)
                if cluster_lead and cluster_p >= MIN_ORIGIN_PROBABILITY:
                    cluster_pick = cluster_lead
                continue
            if cid not in snapshot.components:
                continue
            if cid in investigated and not self._has_new_probe(snapshot, cid, investigated):
                continue
            remaining = [c for c in snapshot.components if c != cid and c not in investigated]
            prior = {c: r for c, r in investigated.items() if c != cid}
            result = self.investigate(snapshot, cid, component, prior, remaining)
            investigated[cid] = result
            confirmed.pop(cid, None)
            step = self._step(len(steps) + 1, cid, result)
            steps.append(step)
            self._trace("decide", "step", **step)
            if result["origin_p"] >= MIN_ORIGIN_PROBABILITY:
                if cid not in checked_other_side:
                    # the other side of a credential or permission cause is examined before anything is concluded
                    checked_other_side.add(cid)
                    others = [
                        c
                        for c, _ in self.other_side(snapshot, cid, result)
                        if c in snapshot.components and c not in investigated
                    ]
                    for other in reversed(others):
                        if other in queue:
                            queue.remove(other)
                        queue.insert(0, other)
                    hypotheses += [c for c in others if c not in hypotheses]
                    if others:
                        self._trace("decide", "other_side", component=cid, others=others)
                confirmed[cid] = result["origin_p"]
                continue
            nxt = result["next"]
            is_victim = result["victim_p"] >= MIN_VICTIM_PROBABILITY
            usable = nxt != NONE_OPTION and nxt not in investigated and eligible(snapshot.components.get(nxt) or {})
            if is_victim and usable and result["next_p"] >= MIN_NEXT_PROBABILITY:
                if nxt in queue:
                    queue.remove(nxt)
                queue.insert(0, nxt)
            elif usable and nxt not in queue and result["next_p"] >= MIN_NEXT_PROBABILITY:
                queue.append(nxt)
            if is_victim:
                # The dependencies this victim's error lines actually name are backups behind Jev's first
                # choice, so a wrong hop does not exhaust the budget before the real dependency is examined.
                # Every other dependency it merely could call stays where triage put it.
                backups = [c for c in result["failing_targets"] if c in snapshot.components]
                insert_at = 1 if queue and queue[0] == nxt else 0
                for c in backups:
                    if not eligible(snapshot.components[c]):
                        continue
                    if c in investigated and self._has_new_probe(snapshot, c, investigated):
                        # A newly discovered failing client changes the backend's
                        # evidence. Reopen that hypothesis within the same step budget.
                        examined.discard(c)
                        if c not in hypotheses:
                            hypotheses.append(c)
                        if c in queue:
                            queue.remove(c)
                        queue.insert(0, c)
                        continue
                    if c not in investigated and c not in queue:
                        queue.insert(insert_at, c)
                        insert_at += 1

        concluded, fallback_reason = self.decide(snapshot, component, investigated, cluster_lead, cluster_p)
        cluster_pick = cluster_lead if concluded == OTHER_OPTION else None
        info = self._characterize(snapshot, concluded, investigated, kind, cluster_pick, fallback_reason)

        probabilities = dict(component.probabilities)
        final = ChoiceResult(
            choice=concluded,
            confidence=round(info["origin_p"], 4) if info["origin_p"] is not None else component.confidence,
            probabilities=probabilities,
        )
        diagnosis = self._diagnosis(snapshot, concluded, final, info, kind)
        diagnosis.investigation = {
            "steps": steps,
            "concluded": concluded,
            "fallback": fallback_reason,
            "triage_top": component.ranked()[:5],
        }
        diagnosis.text = build_report(snapshot, diagnosis)
        self._trace(
            "decide",
            "diagnosis_text",
            component=concluded,
            text=diagnosis.text,
            chars=len(diagnosis.text),
            steps=len(steps),
            fallback=fallback_reason,
            fault_object=info["fault_object"],
        )
        return diagnosis

    def _characterize(
        self,
        snapshot: ClusterSnapshot,
        concluded: str,
        investigated: dict,
        kind: ChoiceResult | None,
        cluster_pick: dict | None,
        fallback_reason: str | None,
    ) -> dict:
        """Category, key evidence, fault object and blocked input of a concluded cause."""
        chosen_result = investigated.get(concluded)
        category = chosen_result["category"] if chosen_result else None
        key = chosen_result["key_evidence"] if chosen_result else None
        evidence = chosen_result["evidence"] if chosen_result else []
        chain_victim = (
            self._chain_victim(snapshot, concluded, investigated)
            if (fallback_reason or "").startswith("end of a victim chain")
            else None
        )
        if chain_victim:
            # The dependency was chosen because a victim fails against it, not because of its own evidence, so
            # its category and key evidence are judged again from the victim's failure.
            category, key, evidence = self.chain_characterization(snapshot, chain_victim, concluded, investigated)
        blocked = None
        if concluded in snapshot.components:
            blocked = snapshot.components[concluded].get("blocked_input")
            if (
                blocked is None
                and chosen_result
                and chosen_result.get("blocked_input")
                and category is not None
                and category.choice in ("data_or_traffic", "code_bug")
            ):
                blocked = chosen_result["blocked_input"]
        reference = None
        if fallback_reason == "verified failed Service reference" or (
            category is not None and category.choice in ("missing_object", "dns_resolution")
        ):
            reference = failed_reference(investigated, {concluded})
        return {
            "unresolved": (fallback_reason or "").startswith("unresolved comparison:"),
            "failed_reference": reference,
            "category": category,
            "key": key,
            "evidence": evidence,
            "origin_p": chosen_result["origin_p"] if chosen_result else None,
            "fault_object": cluster_pick or self.fault_object(snapshot, concluded, category, kind),
            "blocked": blocked,
        }

    def _diagnosis(
        self, snapshot: ClusterSnapshot, cid: str, final: ChoiceResult, info: dict, kind: ChoiceResult | None
    ) -> Diagnosis:
        return Diagnosis(
            component=cid,
            component_result=final,
            fault_visible=None,
            category_result=info["category"],
            evidence_result=info["key"],
            evidence=info["evidence"],
            model=str(self.requests[-1].get("model") or ""),
            input_tokens=_sum(self.requests, "input_tokens"),
            output_tokens=_sum(self.requests, "output_tokens"),
            fault_kind_result=kind,
            fault_object=info["fault_object"],
            blocked_input=info["blocked"],
            trims={},
            unresolved=info.get("unresolved", False),
            failed_reference=info.get("failed_reference"),
        )

    @staticmethod
    def _step(n: int, cid: str, result: dict) -> dict:
        return {
            "step": n,
            "node": "investigate",
            "component": cid,
            "verdict": result["verdict"],
            "origin_p": result["origin_p"],
            "victim_p": result["victim_p"],
            "next": result["next"],
            "next_p": result["next_p"],
            "category": result["category"].choice,
            "spec_checks": result["spec_checks"],
            "blocked_input": result["blocked_input"],
            "key_evidence": next(
                (
                    e["text"]
                    for e in result["evidence"]
                    if result["key_evidence"] and e["id"] == result["key_evidence"].choice
                ),
                None,
            ),
        }

    @staticmethod
    def _chain_victim(snapshot: ClusterSnapshot, dep: str, investigated: dict) -> str | None:
        """The victim whose errors and certainty gave `dep` its support (see support), if any."""
        pointers = [
            (r["victim_p"] * r["next_p"], r["component"])
            for r in investigated.values()
            if r["next"] == dep and r["verdict"] == "victim" and dep in (r.get("failing_targets") or [])
        ]
        return max(pointers)[1] if pointers else None

    def chain_characterization(
        self, snapshot: ClusterSnapshot, victim: str, dep: str, investigated: dict
    ) -> tuple[ChoiceResult, ChoiceResult | None, list[dict]]:
        """Category and key evidence for a dependency concluded as the end of a victim chain.

        The state holds the victim's failure evidence (its error lines, the dependency its errors name, events)
        and the dependency's own evidence, including a probe result when one ran.
        """
        v = investigated[victim]
        d = investigated.get(dep) or {}
        failure_prefixes = ("log x", "repeated failure", "previous log", "event ", "this component's still-occurring")
        items = {}
        for cid, result in ((victim, v), (dep, d)):
            owner = {k: snapshot.components[cid].get(k) for k in ("kind", "namespace", "name")}
            for entry in result.get("evidence") or []:
                if cid == victim and not (entry["text"].startswith(failure_prefixes) or dep in entry["text"]):
                    continue
                text = f"{cid}: {entry['text']}"
                items.setdefault(text, {**entry, "text": text, "source_object": entry.get("source_object", owner)})
        evidence = [{**entry, "id": f"E{i + 1}"} for i, entry in enumerate(list(items.values())[:MAX_EVIDENCE])]
        comp = snapshot.components[dep]
        state = {
            "application": snapshot.app,
            "victim": {"component": victim, "verdict_probabilities": v["verdict_probabilities"]},
            "dependency": {
                "component": dep,
                "kind": comp["kind"],
                "signals": comp.get("signals"),
                "probe": d.get("probe"),
            },
            "evidence": evidence,
        }
        questions: dict[str, Choice] = {
            CATEGORY_QUESTION: Choice(
                instructions={
                    "question": f"`{victim}` fails when calling `{dep}`, and `{dep}` shows no fault in its own collected "
                    f"state. Which category names the fault inside `{dep}` that explains the failures?",
                    "how_to_decide": [
                        "Judge from the victim's error messages and from `dependency.probe` when present. Ordinary "
                        "settings of the dependency (limits, images) are not the cause unless its state shows them "
                        "failing.",
                        "Let the victim's errors decide: refused or failed authentication points at credentials, "
                        "refused connections at the server's listener or network path, timeouts at load or network, "
                        "protocol or parse errors at configuration.",
                    ],
                },
                criteria=FAULT_CATEGORIES,
            )
        }
        selectable = [entry for entry in evidence if not entry.get("context")]
        if selectable:
            questions[EVIDENCE_QUESTION] = Choice(
                instructions={
                    "question": "Which single item in `evidence` most directly shows the failure between the two "
                    "components?",
                    "how_to_decide": ["Prefer a probe result or an error message that names the failing operation."],
                },
                criteria={item["id"]: item["text"] for item in selectable},
            )
        response = self._ask(f"chain:{victim}->{dep}", state, questions)
        category = ChoiceResult.from_answer(response.choices[CATEGORY_QUESTION])
        key = (
            ChoiceResult.from_answer(response.choices[EVIDENCE_QUESTION])
            if EVIDENCE_QUESTION in response.choices
            else None
        )
        self._trace("decide", "chain_characterization", victim=victim, dependency=dep, category=category.ranked()[:3])
        return category, key, evidence

    def support(
        self, snapshot: ClusterSnapshot, component: ChoiceResult, investigated: dict
    ) -> dict[str, tuple[float, str | None]]:
        """Each examined candidate's support for being where the fault started, with how it was reached.

        A candidate's support is its own examination's origin probability. A dependency that a victim's
        still-occurring errors name, and that was not itself judged a victim, gets the victim's certainty times its
        certainty about that dependency when that is higher: a server whose state changed fails its clients while
        looking healthy. The same eligibility rule applies here as in the candidate queue.
        """
        top = component.ranked()[0][0] if component.probabilities else None
        out: dict[str, tuple[float, str | None]] = {}
        for cid, r in investigated.items():
            if eligible(snapshot.components[cid], triage_first=cid == top):
                out[cid] = (r["origin_p"], None)
        for cid, r in investigated.items():
            dep = r["next"]
            if r["verdict"] != "victim" or dep not in out or dep not in (r.get("failing_targets") or []):
                continue
            pointer = r["victim_p"] * r["next_p"]
            if investigated[dep]["verdict"] == "victim" or not shows_own_state(snapshot, dep, investigated[dep]):
                continue
            if pointer > out[dep][0]:
                out[dep] = (pointer, f"end of a victim chain: {cid} fails calling {dep}")
        return out

    def decide(
        self,
        snapshot: ClusterSnapshot,
        component: ChoiceResult,
        investigated: dict,
        cluster_lead: dict | None,
        cluster_p: float,
    ) -> tuple[str, str | None]:
        """The candidate with the most support (the cluster object the cluster question picked counts with its
        probability). The two leaders are compared side by side when their support differs by less than
        NEAR_TIE_MARGIN, or when one is the cluster object and acts on the other; never when both carry the same
        change, which the report states as one cause. Without an examined candidate, triage decides."""
        from clients.jev_diag.v2.report import same_change

        support = self.support(snapshot, component, investigated)
        if cluster_lead is not None:
            support[OTHER_OPTION] = (cluster_p, None)
        ranked = sorted(support.items(), key=lambda kv: -kv[1][0])
        if not ranked:
            return self.fallback(snapshot, component)
        leader, (p1, how1) = ranked[0]
        if (investigated.get(leader) or {}).get("verdict") == "victim":
            # A relative probability gap does not turn a victim into a cause. Prefer a unique,
            # independently verified failed reference from the examinations when one exists.
            if ref := failed_reference(investigated, set(investigated)):
                return ref["dependency"], "verified failed Service reference"
        if len(ranked) == 1:
            return leader, how1
        runner, (p2, how2) = ranked[1]
        pair = {leader, runner}
        acted_on = OTHER_OPTION in pair and bool(
            (pair - {OTHER_OPTION}) & set((cluster_lead or {}).get("components") or [])
        )
        shared = OTHER_OPTION not in pair and runner in same_change(snapshot, leader)
        if shared or (p1 - p2 >= NEAR_TIE_MARGIN and not acted_on):
            return leader, how1
        reason = (
            "the cluster object acts on the other side"
            if acted_on
            else "the two candidates with the most support; their examinations do not separate them"
        )
        winner = self.compare_origins(
            snapshot, {leader: p1, runner: p2}, investigated, reason, cluster_pick=cluster_lead
        )
        if winner is None:
            if ref := failed_reference(investigated, pair):
                return ref["dependency"], "verified failed Service reference"
            origin = investigated.get(leader) or {}
            independent = (
                origin.get("verdict") == "origin" and origin.get("origin_p", 0) >= MIN_ORIGIN_PROBABILITY
            ) or (leader == OTHER_OPTION and cluster_p >= MIN_ORIGIN_PROBABILITY)
            return leader, how1 if independent else "unresolved comparison: no independently supported origin"
        return winner, (how1 if winner == leader else how2)

    def fallback(self, snapshot: ClusterSnapshot, component: ChoiceResult) -> tuple[str, str]:
        """No candidate was examined: `other` when triage chose it and cluster findings exist, else the highest-ranked
        eligible triage candidate."""
        if component.choice == OTHER_OPTION and snapshot.cluster.get("findings"):
            return OTHER_OPTION, "triage chose other"
        for cid, _ in component.ranked():
            if cid == OTHER_OPTION:
                if snapshot.cluster.get("findings"):
                    return cid, "triage top-1"
                continue
            if eligible(snapshot.components.get(cid) or {}, triage_first=cid == component.choice):
                return cid, "triage top-1 eligible candidate"
        return component.choice, "triage top-1"

    def fault_object(
        self, snapshot: ClusterSnapshot, concluded: str, category: ChoiceResult | None, kind: ChoiceResult | None
    ) -> dict | None:
        """The object that carries the fault, chosen by the investigation's cause category.

        For a component conclusion the object must act on the component or be used by it. A value the running pod
        has but its template lacks, set by a matching mutating webhook, makes that webhook the object when the
        rewritten field explains the category. Without an investigation, triage's fault-object kind is used.
        """
        if concluded not in snapshot.components:
            return select_fault_object(snapshot, concluded, kind)
        if category is None:
            return select_fault_object(snapshot, concluded, kind, require_link=True)
        comp = snapshot.components[concluded]
        admission = comp.get("admission_changes") or {}
        markers = _ADMISSION_FIELDS.get(category.choice) or ()
        if admission.get("webhooks") and any(m in d for d in admission.get("differences") or [] for m in markers):
            picked = select_fault_object(
                snapshot,
                concluded,
                ChoiceResult(choice="admission_webhook", confidence=1.0, probabilities={"admission_webhook": 1.0}),
                require_link=True,
            )
            if picked:
                return picked
        object_kind = CATEGORY_TO_OBJECT_KIND.get(category.choice)
        if object_kind is None:
            return None
        return select_fault_object(
            snapshot,
            concluded,
            ChoiceResult(choice=object_kind, confidence=1.0, probabilities={object_kind: 1.0}),
            require_link=True,
        )


def _sum(requests: list[dict], key: str) -> int | None:
    known = [r["usage"].get(key) for r in requests if isinstance(r["usage"].get(key), int)]
    return sum(known) if known else None


def code_only_diagnosis(snapshot: ClusterSnapshot, reason: str) -> Diagnosis:
    """The component code ranks first without the model: the one flagged with the change nearest the symptoms,
    else the unhealthy component with the most non-symptom evidence, telemetry components last."""

    def rank(item: tuple[str, dict]) -> tuple:
        cid, comp = item
        kinds = set(comp.get("evidence_kinds") or [])
        return (
            not eligible(comp),
            not (comp.get("nearest_change_before_symptoms") or comp.get("most_recent_change")),
            "change" not in kinds,
            "configuration" not in kinds,
            {False: 0, None: 1, True: 2}[comp.get("healthy", True)],
            -len(comp.get("signals") or []),
            cid,
        )

    cid = sorted(snapshot.components.items(), key=rank)[0][0]
    result = ChoiceResult(choice=cid, confidence=0.0, probabilities={cid: 0.0})
    diagnosis = Diagnosis(
        component=cid,
        component_result=result,
        fault_visible=None,
        category_result=None,
        evidence_result=None,
        evidence=[],
        model="",
        input_tokens=None,
        output_tokens=None,
        investigation={"steps": [], "concluded": cid, "fallback": f"decision model unavailable: {reason}"},
    )
    diagnosis.text = build_report(snapshot, diagnosis)
    return diagnosis


def reversed_options(questions: dict) -> dict:
    """The same questions with the component options in reverse order (position bias check)."""
    q = questions[COMPONENT_QUESTION]
    flipped = Choice(instructions=q.instructions, criteria=dict(reversed(list(q.criteria.items()))))
    return {**questions, COMPONENT_QUESTION: flipped}


def averaged(results: list[ChoiceResult]) -> ChoiceResult:
    """The mean of several answers to one choice question."""
    keys = list(dict.fromkeys(k for r in results for k in r.probabilities))
    probs = {k: round(sum(r.probabilities.get(k, 0.0) for r in results) / len(results), 4) for k in keys}
    best = max(probs, key=probs.get)
    return ChoiceResult(choice=best, confidence=probs[best], probabilities=probs)
