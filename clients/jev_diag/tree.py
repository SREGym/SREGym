"""Iterative, hypothesis-driven diagnosis: a decision tree where code owns the loop.

Node 1 (triage) ranks components over the compact snapshot, exactly as the
one-shot mode does. Code turns the distribution into a queue of hypotheses.

Node 2 (investigate) takes one component, fetches deep evidence for it
(investigate.collect_component_detail), and asks Jev four narrow questions in
one request: is this the origin, a victim, unrelated, or undetermined; which
component to examine next; the cause category; the single key evidence item.

Code decides the transition: a confirmed origin ends the search, a victim
verdict moves the named dependency to the front of the queue, anything else
moves on. A cluster node handles quotas, webhooks, and DNS. A step budget caps
cost; the best origin score seen is the fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from typesafe_sdk import Choice

from clients.jev_diag.classifier import (
    CATEGORY_QUESTION,
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
    select_fault_object,
)
from clients.jev_diag.collector import ClusterSnapshot, estimate_tokens, fit_state_to_budget

logger = logging.getLogger("all.jev_diag.tree")

VERDICT_QUESTION = "verdict"
NEXT_QUESTION = "next_component"
CLUSTER_OBJECT_QUESTION = "cluster_object"
NONE_OPTION = "none"

DEFAULT_MAX_STEPS = 18
MIN_ORIGIN_PROBABILITY = 0.6  # confirm and stop
STRONG_ORIGIN_PROBABILITY = 0.9  # confirm even when a higher-ranked triage candidate is still unexamined
MIN_VICTIM_PROBABILITY = 0.5  # follow the dependency Jev names
MIN_NEXT_PROBABILITY = 0.3
MIN_QUEUE_PROBABILITY = 0.05  # every triage candidate at least this plausible is examined, budget permitting
MAX_QUEUE = 6
QUEUE_EXTENSION = 4  # extra candidates admitted on evidence alone
# A cause category that names the kind of object carrying the fault more precisely than triage did.
CATEGORY_TO_OBJECT_KIND = {"rbac_permission": "rbac"}
DETAIL_TOKEN_BUDGET = 22000

VERDICTS: dict[str, dict[str, str]] = {
    "origin": {
        "what": "The abnormal signals across the application are explained by `candidate` itself: its own spec, "
        "environment, probes, resources, image, the content of a ConfigMap or Secret it reads, a Service or "
        "NetworkPolicy or quota that targets it, its job definition, its permissions, or its own processing of "
        "its inputs (bad data, permission denials).",
        "not_for": "A component whose only problems are failed calls to another component, or a resource limit "
        "that merely looks small: a limit is the origin only with OOMKilled, throttling at the limit, or eviction "
        "evidence.",
    },
    "victim": {
        "what": "`candidate` misbehaves only because something it calls is failing or refusing it: its errors name "
        "another component, a Service name, an address, or a backend role (database, cache, queue, redis, "
        "broker), and `candidate`'s own configuration shows nothing wrong. The dependency may look healthy "
        "itself: a server whose password, ACL, or config changed shows the failure on its clients, not on itself.",
        "not_for": "A component whose own configuration or referenced config content is abnormal.",
    },
    "unrelated": {
        "what": "`candidate` is healthy, or its abnormalities are settled deploy-time churn or telemetry noise "
        "with no link to the application's fault.",
        "not_for": "Any component with an unexplained abnormal signal of its own.",
    },
    "undetermined": {
        "what": "The evidence does not allow deciding between origin and victim; more of the application must "
        "be examined.",
        "not_for": "Cases where either origin or victim is clearly supported.",
    },
}


def detail_evidence(comp: dict, detail: dict) -> list[dict]:
    """Evidence items for one investigated component: configuration and change first, then logs, then state."""
    items: list[str] = []
    w = detail.get("workload") or {}
    for sig in comp.get("signals") or []:
        if "is terminating" not in sig:
            items.append(sig)
    items += detail.get("spec_checks") or []
    items += detail.get("service_checks") or []
    for key in ("dns_policy", "dns_config", "host_aliases", "host_network", "node_selector", "tolerations", "strategy"):
        if w.get(key) not in (None, "ClusterFirst", False, [], {}):
            items.append(f"workload {key}: {w[key]}")
    for c in w.get("containers", []):
        env = c.get("env") or []
        if env:
            shown = ", ".join(f"{e['name']}={e.get('value', e.get('from', '?'))}" for e in env[:14])
            items.append(f"container {c['name']} env: {shown}")
        if c.get("probes"):
            items.append(f"container {c['name']} probes: {c['probes']}")
        if c.get("args"):
            items.append(f"container {c['name']} args: {' '.join(map(str, c['args']))[:200]}")
        items.append(f"container {c['name']} image {c.get('image')} resources {c.get('resources') or 'none'}")
    for name, cm in (detail.get("configmaps") or {}).items():
        for key, value in (cm.get("data") or {}).items():
            items.append(f"ConfigMap {name} key {key}: {value}")
    rbac = detail.get("rbac") or {}
    for role in rbac.get("roles") or []:
        items.append(
            f"RBAC {role.get('kind')} {role.get('name')} bound via {', '.join(role.get('bindings') or [])} grants: "
            f"{'; '.join(role.get('rules') or []) or 'nothing'}"
            + (f"; modified {role['modified']}" if role.get("seconds_after_deploy") is not None else "")
        )
        for sig in role.get("signals") or []:
            items.append(f"RBAC {role.get('kind')} {role.get('name')}: {sig}")
    if rbac.get("bindings") is not None:
        items.append(f"RBAC bindings for {rbac.get('service_account')}: {rbac.get('bindings') or 'none'}")
    logs = detail.get("logs") or {}
    if logs.get("latest_application_change") and logs.get("error_lines"):
        items.append(
            f"{logs.get('error_lines_after_latest_change') or 0} error log lines after the latest application "
            f"change at {logs['latest_application_change']}, {logs.get('error_lines_before_latest_change') or 0} "
            "before it"
        )
    for s in logs.get("error_samples", [])[:8]:
        when = ""
        if "before_latest_change" in s:
            when = " (before the latest change)" if s["before_latest_change"] else " (after the latest change)"
        items.append(f"log x{s.get('count')}{when}: {s.get('line')}")
    for key, lines in (logs.get("previous_container_logs") or {}).items():
        for ln in lines[-3:]:
            items.append(f"previous log {key}: {ln}")
    for ev in (detail.get("events") or [])[:6]:
        if ev.get("type") == "Warning":
            items.append(f"event {ev.get('reason')} x{ev.get('count')} on {ev.get('object')}: {ev.get('message')}")
    for dep in detail.get("failing_targets") or []:
        items.append(f"this component's error lines refer to its dependency {dep}")
    for cid, info in (detail.get("calls") or {}).items():
        if info.get("own_signals"):
            items.append(f"dependency {cid} is abnormal on its own: {info['own_signals'][0]}")
        elif info.get("looks_healthy_on_its_own"):
            items.append(f"dependency {cid} looks healthy on its own ({info.get('pods_ready')} pods ready)")
    for src, classes in (detail.get("called_by_with_errors") or {}).items():
        items.append(f"errors from {src} name this component: {classes}")
    unique = list(dict.fromkeys(i for i in items if i))[:28]
    return [{"id": f"E{i + 1}", "text": text} for i, text in enumerate(unique)]


def build_investigation_questions(
    snapshot: ClusterSnapshot, cid: str, evidence: list[dict], remaining: list[str]
) -> dict[str, Choice]:
    comp = snapshot.components[cid]
    next_options = {
        other: f'{snapshot.components[other]["kind"]} `{snapshot.components[other]["name"]}` (`components["{other}"]`)'
        for other in remaining
    }
    next_options[NONE_OPTION] = (
        "No further component: `candidate` is the origin, or nothing in the state points elsewhere."
    )
    questions: dict[str, Choice] = {
        VERDICT_QUESTION: Choice(
            instructions={
                "question": f"Is `candidate` ({comp['kind']} `{comp['name']}`) the origin of the injected fault, a "
                "victim of another component, unrelated, or undetermined?",
                "how_to_decide": [
                    "Decide in this order. First, `candidate.detail.spec_checks` and `candidate.detail.service_checks` "
                    "are mismatches code found inside this component's own spec or between its spec and the "
                    "Services it addresses (probe port versus container port, env address versus Service port, "
                    "Service targetPort or selector versus the workload). A mismatch that explains the "
                    "component's symptom makes it the `origin`, whatever its logs say about dependencies.",
                    "Second, look for the mechanism in `candidate.detail`: env values, probe targets, args, "
                    "ConfigMap content, RBAC bindings, dns/hostAliases settings, spec writers and `last_spec_write`, "
                    "quota or webhook rejections in events, and error log classes.",
                    "Third, weigh log errors by time. `candidate.detail.logs.latest_application_change` is the "
                    "most recent change made to the application; error samples marked `before_latest_change: "
                    "true` were logged before it and cannot be caused by it (startup retries, background noise). "
                    "Only errors after the change, or errors of a component that has no timestamps, count toward "
                    "`victim`.",
                    "`candidate.detail.calls` lists components this one talks to (from env values and its error "
                    "logs), each judged from its own state: `looks_healthy_on_its_own`, `own_signals`, `pods_ready`, "
                    "`restarts`. `victim` needs current errors that name a dependency; that dependency may still "
                    "look healthy (a server whose password, ACL, or config changed fails its clients, not itself). "
                    "`candidate.detail.called_by_with_errors` lists components whose errors name this one.",
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
                    "as named by its error messages logged after `candidate.detail.logs.latest_application_change`. "
                    "Map addresses, Service names, and backend roles to the component in `candidate.detail.calls` "
                    "that plays that role (for example an error about redis or a cache maps to the cache component "
                    "in `calls`; `candidate.detail.failing_targets` lists the matches code found). Choose it even "
                    "if it shows no abnormal signals of its own. Errors marked `before_latest_change: true` do not "
                    "name a next hop.",
                    "Second choice: a component in `candidate.detail.called_by_with_errors` or `triage_ranking` "
                    "with abnormal signals that belong to the application, not to telemetry.",
                    "Do not choose a telemetry component (tracing, metrics, log shipping, dashboards) unless "
                    "`candidate`'s failing operation is telemetry export itself.",
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
                ],
            },
            criteria=FAULT_CATEGORIES,
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
                ],
            },
            criteria={item["id"]: item["text"] for item in evidence},
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
                    "a ResourceQuota explains 'failed quota' rejections; a modified CoreDNS ConfigMap explains DNS "
                    "resolution failures across components; a NetworkPolicy explains blocked traffic to the pods "
                    "it selects.",
                ],
            },
            criteria=options,
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
        **kwargs: Any,
    ) -> None:
        super().__init__(client, **kwargs)
        self.fetch_detail = fetch_detail
        self.max_steps = max_steps

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

    def initial_queue(self, snapshot: ClusterSnapshot, component: ChoiceResult) -> list[str]:
        """Plausible triage candidates in triage order, with one exception.

        A telemetry component (tracing, metrics, log shipping) whose only evidence is a change or error logs is
        investigated after every application candidate in the shortlist: harness and operators write to those
        objects routinely, and export errors rarely explain an application fault. A telemetry component with
        symptoms of its own (Pending or failing pods) keeps its triage position. At least one component with
        application-level evidence is queued.
        """
        shortlist: list[str] = []
        for cid, p in component.ranked():
            if len(shortlist) >= MAX_QUEUE:
                break
            if cid == OTHER_OPTION and not snapshot.cluster.get("findings"):
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
        return [c for c in shortlist if c not in demoted] + demoted

    def investigate(
        self, snapshot: ClusterSnapshot, cid: str, triage: ChoiceResult, investigated: dict, remaining: list[str]
    ) -> dict:
        comp = snapshot.components[cid]
        detail = self.fetch_detail(snapshot, cid)
        evidence = detail_evidence(comp, detail)
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
            f"investigate:{cid}", fitted, build_investigation_questions(snapshot, cid, evidence, remaining)
        )
        verdict = ChoiceResult.from_answer(response.choices[VERDICT_QUESTION])
        nxt = ChoiceResult.from_answer(response.choices[NEXT_QUESTION])
        category = ChoiceResult.from_answer(response.choices[CATEGORY_QUESTION])
        key = (
            ChoiceResult.from_answer(response.choices[EVIDENCE_QUESTION])
            if EVIDENCE_QUESTION in response.choices
            else None
        )
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
            "evidence": evidence,
            "detail_tokens": estimate_tokens(fitted),
        }

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

    # ------------------------------------------------------------------ loop

    def diagnose(self, snapshot: ClusterSnapshot) -> Diagnosis:
        if not snapshot.components:
            raise ValueError("No workloads found in the application namespaces; nothing to classify.")
        component, kind, visible = self.triage(snapshot)
        queue = self.initial_queue(snapshot, component)
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
            if result["origin_p"] >= MIN_ORIGIN_PROBABILITY:
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
        if concluded is None:
            if investigated:
                best = max(investigated.values(), key=lambda r: r["origin_p"])
                if best["origin_p"] >= 0.4:
                    concluded, fallback_reason = best["component"], "best origin probability after budget"
            if concluded is None and component.choice == OTHER_OPTION and snapshot.cluster.get("findings"):
                concluded, fallback_reason = OTHER_OPTION, "triage chose other"
            if concluded is None:
                concluded, fallback_reason = component.choice, "triage top-1"
                if concluded == OTHER_OPTION and not snapshot.cluster.get("findings"):
                    concluded = next((c for c, _ in component.ranked() if c != OTHER_OPTION), component.choice)

        chosen_result = investigated.get(concluded)
        category = chosen_result["category"] if chosen_result else None
        key = chosen_result["key_evidence"] if chosen_result else None
        evidence = chosen_result["evidence"] if chosen_result else []
        origin_p = chosen_result["origin_p"] if chosen_result else None
        object_kind = kind
        if chosen_result and chosen_result["category"].choice in CATEGORY_TO_OBJECT_KIND:
            mapped = CATEGORY_TO_OBJECT_KIND[chosen_result["category"].choice]
            object_kind = ChoiceResult(choice=mapped, confidence=1.0, probabilities={mapped: 1.0})
        fault_object = cluster_pick or select_fault_object(snapshot, concluded, object_kind)
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
        )
        return diagnosis


def _sum(requests: list[dict], key: str) -> int | None:
    known = [r["usage"].get(key) for r in requests if isinstance(r["usage"].get(key), int)]
    return sum(known) if known else None
