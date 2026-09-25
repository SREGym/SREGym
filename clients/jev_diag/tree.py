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
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from typesafe_sdk import Choice, Noul

from clients.jev_diag.checks import describe_probe
from clients.jev_diag.classifier import (
    CATEGORY_QUESTION,
    CLUSTER_LEVEL_KINDS,
    COMPONENT_QUESTION,
    EVIDENCE_QUESTION,
    FAULT_CATEGORIES,
    FAULT_KIND_QUESTION,
    FAULT_VISIBLE_QUESTION,
    OTHER_OPTION,
    ChoiceResult,
    Diagnosis,
    JevDiagnoser,
    build_component_questions,
    build_diagnosis_text,
    finding_linked,
    select_fault_object,
)
from clients.jev_diag.collector import ClusterSnapshot, estimate_tokens, fit_state_to_budget

logger = logging.getLogger("all.jev_diag.tree")

VERDICT_QUESTION = "verdict"
NEXT_QUESTION = "next_component"
CLUSTER_OBJECT_QUESTION = "cluster_object"
BLOCKED_QUESTION = "blocked_input"
INPUT_QUESTION = "input_source"
PAIR_QUESTION = "faulty_side"
NONE_OPTION = "none"

DEFAULT_MAX_STEPS = 18
MIN_ORIGIN_PROBABILITY = 0.6  # confirm and stop
STRONG_ORIGIN_PROBABILITY = 0.9  # confirm even when a higher-ranked triage candidate is still unexamined
MIN_VICTIM_PROBABILITY = 0.5  # follow the dependency Jev names
MIN_NEXT_PROBABILITY = 0.3
MIN_QUEUE_PROBABILITY = 0.05  # every triage candidate at least this plausible is examined, budget permitting
MAX_QUEUE = 6
QUEUE_EXTENSION = 4  # extra candidates admitted on evidence alone
CLUSTER_KIND_MASS = 0.3  # triage probability on cluster-level fault kinds that queues the cluster node early
FALLBACK_ORIGIN_PROBABILITY = 0.4
CHAIN_VICTIM_PROBABILITY = 0.9  # a victim this certain, naming its dependency this certainly, ends a chain there
CHAIN_NEXT_PROBABILITY = 0.9
MIN_BLOCKED_PROBABILITY = 0.5
MIN_PAIR_PROBABILITY = 0.5
MAX_NEXT_OPTIONS = 12
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
        "what": "The abnormal signals across the application are explained by `candidate` itself or by an object "
        "that acts on it: its own spec, environment, probes, resources, image, the content of a ConfigMap or "
        "Secret it reads, a Service, traffic policy, NetworkPolicy, quota, admission webhook, or DNS rule that "
        "targets it, its job definition, its permissions, or its own processing of its inputs (bad data, "
        "permission denials).",
        "not_for": "A component whose only problems are failed calls to another component, or a resource limit "
        "that merely looks small: a limit is the origin only with OOMKilled, throttling at the limit, or eviction "
        "evidence.",
    },
    "victim": {
        "what": "`candidate` misbehaves only because something it calls is failing or refusing it: its "
        "still-occurring errors name another component, a Service name, an address, or a backend role "
        "(database, cache, queue, redis, broker), and `candidate`'s own configuration shows nothing wrong. The "
        "dependency may look healthy itself: a server whose password, ACL, or config changed shows the failure on "
        "its clients, not on itself.",
        "not_for": "A component whose own configuration or referenced config content is abnormal.",
    },
    "unrelated": {
        "what": "`candidate` is healthy, or its abnormalities are stopped errors, settled deploy-time churn, or "
        "telemetry noise with no link to the application's fault.",
        "not_for": "Any component with an unexplained abnormal signal of its own.",
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
    w = detail.get("workload") or {}
    changed_env: set[tuple[str, str]] = set()
    tc = detail.get("template_changes") or {}
    for change in tc.get("changes") or []:
        items.append(f"pod template changed in revision {tc.get('revision')}: {change}")
        m = re.match(r"container (\S+) env (\S+) ", change)
        if m:
            changed_env.add((m.group(1), m.group(2)))
    for sig in comp.get("signals") or []:
        if "is terminating" not in sig:
            items.append(sig)
    items += detail.get("spec_checks") or []
    items += detail.get("service_checks") or []
    if detail.get("probe_text"):
        items.append(detail["probe_text"])
    for key in ("dns_policy", "dns_config", "host_aliases", "host_network", "node_selector", "tolerations", "strategy"):
        if w.get(key) not in (None, "ClusterFirst", False, [], {}):
            items.append(f"workload {key}: {w[key]}")
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
            items.append(f"ConfigMap {name} key {key}: {value}")
    rbac = detail.get("rbac") or {}
    for role in rbac.get("roles") or []:
        items.append(
            f"RBAC {role.get('kind')} {role.get('name')} bound via {', '.join(role.get('bindings') or [])} grants: "
            f"{'; '.join(role.get('rules') or []) or 'nothing'}"
            + (f"; modified {role['modified']}" if role.get("changed") else "")
        )
        for sig in role.get("signals") or []:
            items.append(f"RBAC {role.get('kind')} {role.get('name')}: {sig}")
    if rbac.get("bindings") is not None:
        items.append(f"RBAC bindings for {rbac.get('service_account')}: {rbac.get('bindings') or 'none'}")
    logs = detail.get("logs") or {}
    if logs.get("error_lines"):
        items.append(
            f"{logs.get('error_lines_still_occurring') or 0} error log lines still occurring, "
            f"{logs.get('error_lines_stopped') or 0} from errors that stopped"
        )
    for s in logs.get("error_samples", [])[:8]:
        state = s.get("state") or "undated"
        when = f", last seen {s['last_seen_seconds_ago']}s ago" if s.get("last_seen_seconds_ago") is not None else ""
        items.append(f"log x{s.get('count')} ({state}{when}): {s.get('line')}")
    for r in logs.get("repeated_failures") or []:
        items.append(
            f"repeated failure x{r['count']} over {r['span_seconds']}s with unchanged tokens "
            f"{', '.join(r['constant_tokens'])}: {r['line']}"
        )
    for key, lines in (logs.get("previous_container_logs") or {}).items():
        for ln in lines[-3:]:
            items.append(f"previous log {key}: {ln}")
    for ev in (detail.get("events") or [])[:6]:
        if ev.get("type") == "Warning":
            items.append(f"event {ev.get('reason')} x{ev.get('count')} on {ev.get('object')}: {ev.get('message')}")
    for dep in detail.get("failing_targets") or []:
        items.append(f"this component's still-occurring error lines refer to its dependency {dep}")
    for cid, info in (detail.get("calls") or {}).items():
        if info.get("own_signals"):
            items.append(f"dependency {cid} is abnormal on its own: {info['own_signals'][0]}")
        elif info.get("looks_healthy_on_its_own"):
            items.append(f"dependency {cid} looks healthy on its own ({info.get('pods_ready')} pods ready)")
    for src, classes in (detail.get("called_by_with_errors") or {}).items():
        items.append(f"still-occurring errors from {src} name this component: {classes}")
    items += other_env
    unique = list(dict.fromkeys(i for i in items if i))[:MAX_EVIDENCE]
    return [{"id": f"E{i + 1}", "text": text} for i, text in enumerate(unique)]


def linked_components(snapshot: ClusterSnapshot, cid: str, detail: dict) -> list[str]:
    """Components connected to `cid`: those it calls, those that call it or whose errors name it, and those that
    share a Service with it. Telemetry components are left out."""
    comp = snapshot.components[cid]
    linked = set(detail.get("calls") or {}) | set(detail.get("called_by_with_errors") or {})
    linked |= set(detail.get("failing_targets") or []) | set(comp.get("clients") or []) | set(comp.get("_calls") or [])
    for svc in comp.get("services") or []:
        linked |= set(svc.get("selects_multiple_workloads") or [])
    linked.discard(cid)
    return sorted(
        c for c in linked if c in snapshot.components and snapshot.components[c].get("role") != "observability"
    )


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
                "question": f"Is `candidate` ({comp['kind']} `{comp['name']}`) the origin of the injected fault, a "
                "victim of another component, unrelated, or undetermined?",
                "how_to_decide": [
                    "Decide in this order. First, `candidate.detail.spec_checks` and `candidate.detail.service_checks` "
                    "are mismatches code found inside this component's own spec or between its spec and the "
                    "Services it addresses (probe target versus container port, env address versus Service port, "
                    "Service targetPort or selector versus the workloads, a Local traffic policy versus where its "
                    "clients run). A mismatch that explains the component's symptom makes it the `origin`, whatever "
                    "its logs say about dependencies.",
                    "Second, look for the mechanism in `candidate.detail`: `template_changes` (fields that changed "
                    "in the latest pod template revision), `admission_changes` (values in the running pod that its "
                    "template does not have), env values, probe targets, args, ConfigMap content, RBAC bindings, "
                    "dns/hostAliases settings, quota or webhook rejections in events, and error log classes.",
                    "Third, only errors that are still occurring count. Each error sample has `state`: `ongoing` or "
                    "`stopped` (silent for much longer than its usual gap: start-up retries and past episodes). "
                    "Stopped errors are not evidence for `victim` and do not name a next hop.",
                    "`candidate.detail.calls` lists components this one talks to (from env values and its error "
                    "logs), each judged from its own state: `looks_healthy_on_its_own`, `own_signals`, `pods_ready`, "
                    "`restarts`. `victim` needs still-occurring errors that name a dependency; that dependency may "
                    "still look healthy (a server whose password, ACL, or config changed fails its clients, not "
                    "itself). `candidate.detail.called_by_with_errors` lists components whose errors name this one. "
                    "`candidate.detail.probe`, when present, is the result of a health command run inside this "
                    "component.",
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
                    "to the component in `candidate.detail.calls` that plays that role (for example an error about "
                    "redis or a cache maps to the cache component in `calls`; `candidate.detail.failing_targets` "
                    "lists the matches code found). Choose it even if it shows no abnormal signals of its own.",
                    "Second choice: a component in `candidate.detail.called_by_with_errors`, `candidate.detail.clients`, "
                    "or `triage_ranking` with abnormal signals of its own.",
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
    if evidence:
        questions[EVIDENCE_QUESTION] = Choice(
            instructions={
                "question": "Which single item in `evidence` most directly reveals the mechanism of the fault, "
                "assuming `candidate` is the origin?",
                "how_to_decide": [
                    "Prefer an item naming a configuration value, config content, policy, permission, or change over "
                    "an item describing a state (Pending, not ready, restarts).",
                    "Prefer an item with one specific value over an item listing many.",
                ],
            },
            criteria={item["id"]: item["text"] for item in evidence},
        )
    candidates = detail.get("input_candidates") or []
    if candidates:
        options = {
            f"I{i + 1}": f"env {c['env']}={c['value']} (container {c['container']})" for i, c in enumerate(candidates)
        }
        options[NONE_OPTION] = "None of these values names where the input comes from."
        questions[INPUT_QUESTION] = Choice(
            instructions="If `candidate` reads its input from a queue, topic, stream, or table, which value in "
            "`candidate.detail.input_candidates` names that source?",
            criteria=options,
        )
    return questions


def build_cluster_questions(findings: list[dict]) -> dict[str, Choice]:
    options = {f"F{i + 1}": f"{f.get('kind')} {f.get('name')}: {f.get('signal')}" for i, f in enumerate(findings)}
    options[NONE_OPTION] = "None of these objects explains the affected workloads."
    return {
        CLUSTER_OBJECT_QUESTION: Choice(
            instructions={
                "question": "Which cluster-level or namespace-level object in `findings` explains the abnormal "
                "signals of the `affected_components`?",
                "how_to_decide": [
                    "A webhook with no backend and failurePolicy Fail explains FailedCreate events that mention it; "
                    "a mutating webhook that rewrote a workload's pods explains values the pods have but their "
                    "template lacks; a ResourceQuota explains 'failed quota' rejections; a CoreDNS rule for a "
                    "Service explains resolution failures of that name; a NetworkPolicy explains blocked traffic to "
                    "the pods it selects; a role explains authorization denials of the workloads bound to it; an "
                    "object stuck in Terminating points at the controller that should clear its finalizer.",
                    "`findings[].components` lists the workloads an object is known to act on.",
                ],
            },
            criteria=options,
        )
    }


def build_pair_questions(a: str, b: str, reason: str) -> dict[str, Choice]:
    return {
        PAIR_QUESTION: Choice(
            instructions={
                "question": f"{reason}. Which side holds the wrong or stale setting that has to change for the two "
                "to work together again?",
                "how_to_decide": [
                    "Prefer the side whose own configuration changed or became stale over the side that only reports "
                    "the failure.",
                    "A server that rejects logins reports the credentials its clients present; it holds the fault only "
                    "if its own expected credentials changed while the client's stayed valid.",
                    "A client that reads credentials through env values keeps presenting the values it started with "
                    "after their source is rewritten; mounted files are updated in place.",
                ],
            },
            criteria={
                a: f'`sides["{a}"]` holds the faulty setting.',
                b: f'`sides["{b}"]` holds the faulty setting.',
                NONE_OPTION: "Neither side's state explains it; something between them does.",
            },
        )
    }


class IterativeDiagnoser(JevDiagnoser):
    """Decision-tree diagnosis on top of the one-shot triage."""

    def __init__(
        self,
        client=None,
        *,
        fetch_detail: Callable[[ClusterSnapshot, str], dict],
        max_steps: int = DEFAULT_MAX_STEPS,
        probe: Callable[[ClusterSnapshot, str, str | None], dict | None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(client, **kwargs)
        self.fetch_detail = fetch_detail
        self.max_steps = max_steps
        self.probe = probe

    # ------------------------------------------------------------------ nodes

    def triage(self, snapshot: ClusterSnapshot) -> tuple[ChoiceResult, ChoiceResult | None, float | None]:
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
        response = self._ask("triage", state, build_component_questions(snapshot))
        component = ChoiceResult.from_answer(response.choices[COMPONENT_QUESTION])
        kind = (
            ChoiceResult.from_answer(response.choices[FAULT_KIND_QUESTION])
            if FAULT_KIND_QUESTION in response.choices
            else None
        )
        visible = response.nouls[FAULT_VISIBLE_QUESTION].noul if FAULT_VISIBLE_QUESTION in response.nouls else None
        return component, kind, visible

    def initial_queue(
        self, snapshot: ClusterSnapshot, component: ChoiceResult, kind: ChoiceResult | None = None
    ) -> list[str]:
        """Plausible triage candidates in triage order, with two adjustments.

        A telemetry component (tracing, metrics, log shipping) whose only evidence is a change or error logs is
        investigated after every application candidate in the shortlist: harness and operators write to those
        objects routinely, and export errors rarely explain an application fault. A telemetry component with
        symptoms of its own (Pending or failing pods) keeps its triage position. When triage puts at least
        CLUSTER_KIND_MASS of its fault-kind probability on cluster-level objects, the cluster node is examined
        right after the first candidate.
        """
        findings = snapshot.cluster.get("findings")
        shortlist: list[str] = []
        for cid, p in component.ranked():
            if len(shortlist) >= MAX_QUEUE:
                break
            if cid == OTHER_OPTION and not findings:
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
            if cid not in shortlist and comp.get("role") != "observability" and kinds & set(priority):
                rank = min(priority[k] for k in kinds if k in priority)
                extra.append((rank, -component.probabilities.get(cid, 0.0), cid))
        for _, _, cid in sorted(extra)[: max(0, MAX_QUEUE + QUEUE_EXTENSION - len(shortlist))]:
            shortlist.append(cid)

        def demote(cid: str) -> bool:
            comp = snapshot.components.get(cid) or {}
            kinds = set(comp.get("evidence_kinds") or [])
            return comp.get("role") == "observability" and not kinds & {"symptoms", "own_failure_logs", "configuration"}

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
                if (
                    other in remaining
                    and other not in options
                    and snapshot.components[other].get("role") != "observability"
                ):
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
            "triage_ranking": [{"component": c, "probability": p} for c, p in triage.ranked()[:8]],
            "investigated_so_far": [
                {"component": c, "verdict": r["verdict"], "origin_probability": r["origin_p"]}
                for c, r in investigated.items()
            ],
            "cluster_findings": snapshot.cluster.get("findings") or [],
            "components": {},
        }
        fitted, trims = fit_state_to_budget(state, DETAIL_TOKEN_BUDGET)
        fitted.pop("components", None)
        self._trace(
            "jev",
            "budget_fit",
            label=f"investigate:{cid}",
            budget=DETAIL_TOKEN_BUDGET,
            tokens_before=estimate_tokens(state),
            tokens_after=estimate_tokens(fitted),
            applied=trims,
        )
        response = self._ask(
            f"investigate:{cid}", fitted, build_investigation_questions(snapshot, cid, evidence, options, detail)
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
        source = (
            ChoiceResult.from_answer(response.choices[INPUT_QUESTION]) if INPUT_QUESTION in response.choices else None
        )
        key_text = next((e["text"] for e in evidence if key and e["id"] == key.choice), None)
        blocked = None
        if blocked_p is not None and blocked_p >= MIN_BLOCKED_PROBABILITY:
            repeated = (detail.get("logs") or {}).get("repeated_failures") or []
            blocked = {
                "source": "logs",
                "evidence": repeated[0]["line"] if repeated else key_text,
                "probability": round(float(blocked_p), 4),
            }
            if source and source.choice != NONE_OPTION:
                picked = (detail.get("input_candidates") or [])[int(source.choice[1:]) - 1]
                blocked["input"] = f"env {picked['env']}={picked['value']}"
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
            "evidence": evidence,
            "detail_tokens": estimate_tokens(fitted),
        }

    def _maybe_probe(self, snapshot: ClusterSnapshot, cid: str, detail: dict, investigated: dict) -> None:
        """Run a health command inside a healthy-looking component that an investigated victim's errors name."""
        if self.probe is None:
            return
        clients = [
            c
            for c, r in investigated.items()
            if r["victim_p"] >= MIN_VICTIM_PROBABILITY and cid in (r.get("failing_targets") or [])
        ]
        comp = snapshot.components[cid]
        own = [s for s in comp.get("signals") or [] if not s.startswith(("errors logged by", "server "))]
        pods = comp.get("pods") or []
        healthy = bool(pods) and all(
            p.get("ready", "0/0").split("/")[0] == p.get("ready", "0/0").split("/")[1] for p in pods
        )
        if not clients or own or not healthy:
            return
        try:
            result = self.probe(snapshot, cid, clients[0])
        except Exception as exc:  # noqa: BLE001 - a probe never stops the diagnosis
            self._trace("collect", "probe", component=cid, error=f"{type(exc).__name__}: {exc}")
            return
        self._trace("collect", "probe", component=cid, client=clients[0], result=result)
        if result:
            from clients.jev_diag.probes import describe_probe_result

            detail["probe"] = result
            detail["probe_text"] = describe_probe_result(cid, result)

    def cluster_node(self, snapshot: ClusterSnapshot) -> dict | None:
        findings = snapshot.cluster.get("findings") or []
        if not findings:
            return None
        affected = {
            cid: {"signals": c.get("signals"), "warning_events": (c.get("warning_events") or [])[:3]}
            for cid, c in snapshot.components.items()
            if c.get("signals") and c.get("role") != "observability"
        }
        state = {
            "application": snapshot.app,
            "findings": [{"id": f"F{i + 1}", **f} for i, f in enumerate(findings)],
            "affected_components": affected,
            "cluster_dns": snapshot.cluster.get("cluster_dns"),
        }
        fitted, _ = fit_state_to_budget({**state, "components": {}}, DETAIL_TOKEN_BUDGET)
        fitted.pop("components", None)
        response = self._ask("cluster_objects", fitted, build_cluster_questions(findings))
        result = ChoiceResult.from_answer(response.choices[CLUSTER_OBJECT_QUESTION])
        chosen = None
        if result.choice != NONE_OPTION:
            chosen = findings[int(result.choice[1:]) - 1]
        return {"result": result, "object": chosen}

    def pair_node(self, snapshot: ClusterSnapshot, a: str, b: str, investigated: dict, reason: str) -> str:
        """Ask which of two confirmed origins holds the faulty setting; fall back to the higher origin probability."""

        def side(cid: str) -> dict:
            r = investigated[cid]
            comp = snapshot.components[cid]
            key = next(
                (e["text"] for e in r["evidence"] if r["key_evidence"] and e["id"] == r["key_evidence"].choice), None
            )
            return {
                "component": cid,
                "kind": comp["kind"],
                "origin_probability": r["origin_p"],
                "cause_category": r["category"].choice,
                "key_evidence": key,
                "code_checks": r["spec_checks"],
                "signals": (comp.get("signals") or [])[:12],
                "config_objects": comp.get("config_objects"),
            }

        state = {"application": snapshot.app, "connection": reason, "sides": {a: side(a), b: side(b)}}
        response = self._ask(f"pair:{a}|{b}", state, build_pair_questions(a, b, reason))
        answer = ChoiceResult.from_answer(response.choices[PAIR_QUESTION])
        winner = (
            answer.choice
            if answer.choice in (a, b) and answer.probabilities.get(answer.choice, 0) >= MIN_PAIR_PROBABILITY
            else None
        )
        if winner is None:
            winner = max((a, b), key=lambda c: investigated[c]["origin_p"])
        self._trace("decide", "pair", sides=[a, b], reason=reason, ranked=answer.ranked(), winner=winner)
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
        if not snapshot.components:
            raise ValueError("No workloads found in the application namespaces; nothing to classify.")
        component, kind, visible = self.triage(snapshot)
        queue = self.initial_queue(snapshot, component, kind)
        self._trace(
            "decide",
            "component",
            chosen=component.choice,
            confidence=component.confidence,
            ranked=component.ranked()[:8],
            fault_kind=kind.choice if kind else None,
            fault_visible=visible,
            queue=list(queue),
            mode="tree",
        )

        investigated: dict[str, dict] = {}
        steps: list[dict] = []
        concluded: str | None = None
        cluster_pick: dict | None = None
        # other side -> (component waiting to be concluded, reason, further candidates to examine after this one)
        waiting_on: dict[str, tuple[str, str, list[tuple[str, str]]]] = {}
        checked_other_side: set[str] = set()
        while queue and len(steps) < self.max_steps:
            cid = queue.pop(0)
            if cid == OTHER_OPTION:
                node = self.cluster_node(snapshot)
                steps.append(
                    {"step": len(steps) + 1, "node": "cluster", "result": node["result"].ranked()[:3] if node else None}
                )
                self._trace("decide", "step", **steps[-1])
                if (
                    node
                    and node["object"]
                    and node["result"].probabilities.get(node["result"].choice, 0) >= MIN_ORIGIN_PROBABILITY
                ):
                    cluster_pick = node["object"]
                    concluded = OTHER_OPTION
                    break
                continue
            if cid not in snapshot.components or cid in investigated:
                continue
            remaining = [c for c in snapshot.components if c != cid and c not in investigated]
            result = self.investigate(snapshot, cid, component, investigated, remaining)
            investigated[cid] = result
            step = {
                "step": len(steps) + 1,
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
            steps.append(step)
            self._trace("decide", "step", **step)
            if cid in waiting_on:
                # This was the other side of an earlier confirmed origin: settle between the two, or examine the
                # next candidate when this one is not an origin.
                original, reason, rest = waiting_on.pop(cid)
                if result["origin_p"] >= MIN_ORIGIN_PROBABILITY:
                    concluded = self.pair_node(snapshot, original, cid, investigated, reason)
                    break
                rest = [(c, r) for c, r in rest if c in snapshot.components and c not in investigated]
                if rest:
                    nxt_side, nxt_reason = rest[0]
                    waiting_on[nxt_side] = (original, nxt_reason, rest[1:])
                    if nxt_side in queue:
                        queue.remove(nxt_side)
                    queue.insert(0, nxt_side)
                    self._trace("decide", "defer_for_other_side", component=original, other=nxt_side, reason=nxt_reason)
                    continue
                concluded = original
                break
            if result["origin_p"] >= MIN_ORIGIN_PROBABILITY:
                sides = [
                    (c, r)
                    for c, r in self.other_side(snapshot, cid, result)
                    if c in snapshot.components and c not in investigated
                ]
                if sides and cid not in checked_other_side:
                    checked_other_side.add(cid)
                    other, reason = sides[0]
                    waiting_on[other] = (cid, reason, sides[1:])
                    if other in queue:
                        queue.remove(other)
                    queue.insert(0, other)
                    self._trace("decide", "defer_for_other_side", component=cid, other=other, reason=reason)
                    continue
                # A confirmed origin ends the search unless triage ranked an unexamined component higher: that
                # component is examined first, and this one stays the fallback if nothing better is found.
                higher = [
                    c
                    for c, p in component.ranked()
                    if c != cid
                    and c in snapshot.components
                    and c not in investigated
                    and p > component.probabilities.get(cid, 0.0)
                ]
                if not higher or result["origin_p"] >= STRONG_ORIGIN_PROBABILITY:
                    concluded = cid
                    break
                for c in reversed(higher[:2]):
                    if c in queue:
                        queue.remove(c)
                    queue.insert(0, c)
                self._trace("decide", "defer_conclusion", component=cid, origin_p=result["origin_p"], first=higher[:2])
                continue
            nxt = result["next"]
            is_victim = result["victim_p"] >= MIN_VICTIM_PROBABILITY
            usable = nxt != NONE_OPTION and nxt in snapshot.components and nxt not in investigated
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
                    if (
                        c not in investigated
                        and c not in queue
                        and snapshot.components[c].get("role") != "observability"
                    ):
                        queue.insert(insert_at, c)
                        insert_at += 1

        fallback_reason = None
        if concluded is None and waiting_on:
            # The budget ran out before the other side was examined: the confirmed origin stands.
            concluded = next(iter(waiting_on.values()))[0]
            fallback_reason = "confirmed origin; the other side was not examined within the budget"
        if concluded is None:
            concluded, fallback_reason = self.fallback(snapshot, component, investigated)

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
        origin_p = chosen_result["origin_p"] if chosen_result else None
        fault_object = cluster_pick or self.fault_object(snapshot, concluded, category, kind)
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
        probabilities = dict(component.probabilities)
        final = ChoiceResult(
            choice=concluded,
            confidence=round(origin_p, 4) if origin_p is not None else component.confidence,
            probabilities=probabilities,
        )
        diagnosis = Diagnosis(
            component=concluded,
            component_result=final,
            fault_visible=None if visible is None else round(float(visible), 4),
            category_result=category,
            evidence_result=key,
            evidence=evidence,
            model=str(self.requests[-1].get("model") or ""),
            input_tokens=_sum(self.requests, "input_tokens"),
            output_tokens=_sum(self.requests, "output_tokens"),
            fault_kind_result=kind,
            fault_object=fault_object,
            blocked_input=blocked,
            trims={},
            investigation={
                "steps": steps,
                "concluded": concluded,
                "fallback": fallback_reason,
                "triage_top": component.ranked()[:5],
            },
        )
        diagnosis.text = build_diagnosis_text(snapshot, diagnosis)
        self._trace(
            "decide",
            "diagnosis_text",
            component=concluded,
            text=diagnosis.text,
            chars=len(diagnosis.text),
            steps=len(steps),
            fallback=fallback_reason,
            fault_object=fault_object,
        )
        return diagnosis

    @staticmethod
    def _chain_victim(snapshot: ClusterSnapshot, dep: str, investigated: dict) -> str | None:
        """The victim whose strong verdict and error lines made `dep` the end of a victim chain, if any."""
        for r in investigated.values():
            if (
                r["next"] == dep
                and r["verdict"] == "victim"
                and r["victim_p"] >= CHAIN_VICTIM_PROBABILITY
                and r["next_p"] >= CHAIN_NEXT_PROBABILITY
                and dep in (r.get("failing_targets") or [])
                and dep in investigated
                and investigated[dep]["verdict"] != "victim"
            ):
                return r["component"]
        return None

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
        items = [
            f"{victim}: {e['text']}"
            for e in v["evidence"]
            if e["text"].startswith(failure_prefixes) or dep in e["text"]
        ]
        items += [f"{dep}: {e['text']}" for e in d.get("evidence") or []]
        unique = list(dict.fromkeys(items))[:MAX_EVIDENCE]
        evidence = [{"id": f"E{i + 1}", "text": text} for i, text in enumerate(unique)]
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
        if evidence:
            questions[EVIDENCE_QUESTION] = Choice(
                instructions={
                    "question": "Which single item in `evidence` most directly shows the failure between the two "
                    "components?",
                    "how_to_decide": ["Prefer a probe result or an error message that names the failing operation."],
                },
                criteria={item["id"]: item["text"] for item in evidence},
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

    def fallback(self, snapshot: ClusterSnapshot, component: ChoiceResult, investigated: dict) -> tuple[str, str]:
        """No origin was confirmed within the budget. In order:

        1. the investigated component with the highest origin probability, among those whose verdict was
           `origin`, if at least FALLBACK_ORIGIN_PROBABILITY;
        2. the end of a strong victim chain: a victim (at least CHAIN_VICTIM_PROBABILITY) whose still-occurring
           error lines name the dependency Jev chose next (at least CHAIN_NEXT_PROBABILITY), when that dependency
           was examined and was not itself a victim, since a server whose state changed fails its clients while
           looking healthy;
        3. `other`, when triage chose it and cluster findings exist;
        4. the highest-ranked triage candidate that is not a telemetry component.
        """
        origins = [
            r
            for r in investigated.values()
            if r["verdict"] == "origin" and r["origin_p"] >= FALLBACK_ORIGIN_PROBABILITY
        ]
        if origins:
            return max(origins, key=lambda r: r["origin_p"])[
                "component"
            ], "most likely origin among investigated components"
        for r in investigated.values():
            dep = r["next"]
            if (
                r["verdict"] == "victim"
                and r["victim_p"] >= CHAIN_VICTIM_PROBABILITY
                and r["next_p"] >= CHAIN_NEXT_PROBABILITY
                and dep in (r.get("failing_targets") or [])
                and dep in investigated
                and investigated[dep]["verdict"] != "victim"
                and (snapshot.components.get(dep) or {}).get("role") != "observability"
            ):
                return dep, f"end of a victim chain: {r['component']} fails calling {dep}"
        if component.choice == OTHER_OPTION and snapshot.cluster.get("findings"):
            return OTHER_OPTION, "triage chose other"
        for cid, _ in component.ranked():
            if cid == OTHER_OPTION:
                if snapshot.cluster.get("findings"):
                    return cid, "triage top-1"
                continue
            if (snapshot.components.get(cid) or {}).get("role") != "observability":
                return cid, "triage top-1 (telemetry components skipped)"
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
