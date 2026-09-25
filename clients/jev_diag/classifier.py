"""Jev (TypeSafe System One) questions for root-cause component classification.

Code owns the workflow: the collector decides what evidence exists, this module
asks Jev a few narrow, typed judgments and turns the typed answers into a
diagnosis text deterministically.

Request 1 (all components, in parallel): which component is the origin of the
fault; what kind of Kubernetes object carries the injected change; whether any
fault is visible at all. Request 2 (chosen component only): which fault
category, and which single evidence item best explains it. The second request
needs the first answer to build its state, which is the one case where a second
round-trip is warranted.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient

from clients.jev_diag.collector import ClusterSnapshot, estimate_tokens, fit_state_to_budget
from clients.jev_diag.config import jev_model
from clients.jev_diag.trace import DecisionTrace

logger = logging.getLogger("all.jev_diag.classifier")

# Jev 1.13 limits: 64k tokens for state + all questions, 32k for state + longest question.
DEFAULT_STATE_TOKEN_BUDGET = int(os.getenv("JEV_DIAG_STATE_TOKEN_BUDGET", "24000"))
DETAIL_STATE_TOKEN_BUDGET = 20000
MAX_EVIDENCE_ITEMS = 24
LOW_CONFIDENCE = 0.3
RUNNER_UP_MIN_PROBABILITY = 0.25

OTHER_OPTION = "other"
COMPONENT_QUESTION = "root_cause_component"
FAULT_KIND_QUESTION = "fault_object_kind"
FAULT_VISIBLE_QUESTION = "fault_visible"
CATEGORY_QUESTION = "fault_category"
EVIDENCE_QUESTION = "key_evidence"

# What kind of Kubernetes object carries the injected change. Cluster-level kinds are the ones a
# workload-only answer cannot express; code uses them to name the object when `other` is chosen.
FAULT_OBJECT_KINDS: dict[str, str] = {
    "workload_spec": "A Deployment, StatefulSet, DaemonSet, CronJob, or Job spec: containers, env, image, "
    "resources, probes, volumes, scheduling constraints, replicas, rollout strategy, dnsPolicy, hostAliases.",
    "service": "A Service object: its selector, ports, or traffic policy.",
    "network_policy": "A NetworkPolicy that blocks or restricts traffic.",
    "config_object": "The content of a ConfigMap or Secret that a workload reads (credentials, config files).",
    "namespace_policy": "A ResourceQuota or LimitRange that rejects or alters pods in the namespace.",
    "admission_webhook": "A Mutating or ValidatingWebhookConfiguration that rejects or rewrites objects.",
    "rbac": "A Role, ClusterRole, or binding: a component is denied API operations it needs (403 forbidden).",
    "node_or_cluster": "A node condition, taint, cordon, cluster DNS (CoreDNS) configuration, or other "
    "cluster-level infrastructure problem.",
    "data_or_traffic": "Not a Kubernetes object: bad data or messages, a load pattern, a retry storm, or a "
    "runtime feature flag inside the application.",
    "not_visible": "The collected state shows no change or abnormality that identifies the object.",
}
CLUSTER_LEVEL_KINDS = {"namespace_policy", "admission_webhook", "rbac", "node_or_cluster", "network_policy"}

# Cause-oriented categories. Pods Pending, not ready, restarting, or terminating are symptoms; each
# category names the thing that made them so.
FAULT_CATEGORIES: dict[str, dict[str, Any]] = {
    "config_value": {
        "what": "A wrong, missing, duplicated, or shadowed value in the component's own spec or configuration "
        "files: environment variables, command arguments, an address or port it uses to reach another component, "
        "config file content, or a setting that makes it consume more CPU or memory than before.",
        "not_for": "Credentials (credentials_or_auth), probes (health_probe_config), resources (resource_limits), "
        "DNS settings (dns_resolution), a Service object's selector or ports (service_routing).",
    },
    "credentials_or_auth": {
        "what": "Authentication between components fails: rotated or wrong credentials in a Secret, a password "
        "or auth requirement changed on a backend, stale credentials in running pods.",
        "not_for": "Kubernetes API authorization (rbac_permission).",
    },
    "image_or_binary": {
        "what": "The container image or binary is wrong: bad image name or tag, ImagePullBackOff, exec format "
        "error, a binary that crashes immediately regardless of configuration.",
        "not_for": "A crash caused by a config value or a failing dependency.",
    },
    "resource_limits": {
        "what": "CPU or memory requests or limits set too low for the workload's normal needs: throttling, "
        "OOMKilled, eviction, or a request too large to schedule.",
        "not_for": "A namespace quota that rejects pods, or values rewritten at admission "
        "(admission_or_namespace_policy); a configuration change that makes the workload consume more "
        "(config_value).",
    },
    "scheduling_constraint": {
        "what": "Pods cannot be placed: nodeSelector, affinity, anti-affinity, tolerations, or taints that no "
        "node satisfies, or a volume that cannot be bound on any eligible node.",
        "not_for": "Pods rejected before scheduling by a quota or webhook (admission_or_namespace_policy).",
    },
    "rollout_or_replica_config": {
        "what": "Replica or rollout settings that stop the workload from running: replicas set to zero, a "
        "rolling update strategy that takes every pod down or can never progress, HPA misconfiguration.",
        "not_for": "Pods that exist but fail for another reason.",
    },
    "health_probe_config": {
        "what": "A readiness, liveness, or startup probe points at the wrong path, port, or command, so "
        "healthy containers are reported not ready or are restarted.",
        "not_for": "Probes that fail because the application really is broken.",
    },
    "service_routing": {
        "what": "A Service sends traffic to the wrong place or nowhere: its selector matches the wrong pods or "
        "none, or its port or targetPort does not match the pods' container ports.",
        "not_for": "A client configured with the wrong address or port (config_value).",
    },
    "network_policy": {
        "what": "A NetworkPolicy blocks traffic to or from the component.",
        "not_for": "Traffic that reaches the component and is then rejected or fails.",
    },
    "traffic_policy": {
        "what": "A Service traffic policy (internalTrafficPolicy or externalTrafficPolicy set to Local) or "
        "session affinity restricts which endpoints receive traffic, so some clients reach no endpoint.",
        "not_for": "A selector or port mismatch (service_routing).",
    },
    "dns_resolution": {
        "what": "Name resolution fails or returns wrong answers: the pod's dnsPolicy, dnsConfig, or hostAliases, "
        "or the cluster DNS (CoreDNS) configuration.",
        "not_for": "A name that resolves but whose backend refuses connections.",
    },
    "storage_volume": {
        "what": "Persistent volume problems: PVC Pending or Lost, a volume mounted twice or at the wrong path, "
        "mount failures, disk full.",
        "not_for": "ConfigMap or Secret mounts with wrong content (config_value).",
    },
    "job_lifecycle": {
        "what": "A Job or CronJob does not complete or runs wrongly: a sidecar keeps the pod alive after the "
        "main container exits, jobs pile up, restartPolicy or backoffLimit or schedule is wrong.",
        "not_for": "A job whose main container itself fails for another category's reason.",
    },
    "admission_or_namespace_policy": {
        "what": "An admission webhook or namespace policy rejects or rewrites pods: a webhook with no "
        "backend and failurePolicy Fail, a mutating webhook injecting bad values, a ResourceQuota or "
        "LimitRange that rejects pods.",
        "not_for": "Scheduling failures of pods that were admitted (scheduling_constraint).",
    },
    "rbac_permission": {
        "what": "The component is denied Kubernetes API operations it needs (403 forbidden) because a Role, "
        "ClusterRole, or binding was removed or narrowed.",
        "not_for": "Authentication between application components (credentials_or_auth).",
    },
    "data_or_traffic": {
        "what": "The component's inputs are the problem: a poison message it cannot process, a retry or "
        "timeout feedback loop, a load pattern or a request filter that saturates it.",
        "not_for": "Failures explained by the component's own spec or a Kubernetes object.",
    },
    "code_bug": {
        "what": "The component runs with correct configuration, resources, and inputs, and still fails "
        "because of its own logic.",
        "not_for": "Anything explained by another category.",
    },
    OTHER_OPTION: {"what": "None of the categories above describes the fault."},
}

CATEGORY_GLOSS: dict[str, str] = {
    "config_value": "a wrong, missing, or shadowed value in this component's own configuration",
    "credentials_or_auth": "authentication between components fails because credentials or auth settings changed",
    "image_or_binary": "the container image or binary is wrong",
    "resource_limits": "CPU or memory requests/limits do not fit the workload",
    "scheduling_constraint": "no node satisfies the pod's placement constraints",
    "rollout_or_replica_config": "replica or rollout settings stop the workload from running",
    "health_probe_config": "a health probe is misconfigured, so healthy containers are reported unhealthy",
    "service_routing": "a Service selector or port sends traffic to the wrong pods or to none",
    "network_policy": "a NetworkPolicy blocks traffic to or from this component",
    "traffic_policy": "a Service traffic policy leaves some clients without a reachable endpoint",
    "dns_resolution": "name resolution fails or returns wrong answers",
    "storage_volume": "a persistent volume or mount problem",
    "job_lifecycle": "the job pod cannot complete or the job runs wrongly",
    "admission_or_namespace_policy": "an admission webhook or namespace policy rejects or rewrites its pods",
    "rbac_permission": "the component is denied Kubernetes API operations it needs",
    "data_or_traffic": "the component's inputs or traffic pattern overwhelm or break it",
    "code_bug": "the component's own logic is faulty",
    OTHER_OPTION: "an uncategorised fault",
}


class SystemOneClient(Protocol):
    """The slice of TypeSafeClient this module uses; a fake can stand in during tests."""

    def system_one(self, state: Any, questions: Any, **kwargs: Any) -> Any: ...


@dataclass
class ChoiceResult:
    choice: str
    confidence: float
    probabilities: dict[str, float]

    @classmethod
    def from_answer(cls, answer: Any) -> ChoiceResult:
        probs = {k: round(float(v), 4) for k, v in dict(answer.probabilities).items()}
        return cls(choice=answer.choice, confidence=round(float(answer.confidence), 4), probabilities=probs)

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])


@dataclass
class Diagnosis:
    component: str
    component_result: ChoiceResult
    fault_visible: float | None
    category_result: ChoiceResult | None
    evidence_result: ChoiceResult | None
    evidence: list[dict]
    model: str
    input_tokens: int | None
    output_tokens: int | None
    fault_kind_result: ChoiceResult | None = None
    fault_object: dict | None = None
    blocked_input: dict | None = None  # tree mode: a single input item that blocks the component, when found
    trims: dict[str, list[str]] = field(default_factory=dict)
    investigation: dict | None = None  # decision-tree mode: steps, conclusion, fallback
    text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- questions


def component_options(snapshot: ClusterSnapshot) -> dict[str, str]:
    options = {}
    for cid, comp in snapshot.components.items():
        role = " (observability/telemetry component)" if comp.get("role") == "observability" else ""
        options[cid] = (
            f"{comp['kind']} `{comp['name']}` in namespace `{comp['namespace']}`{role}; its collected state is "
            f'`components["{cid}"]`.'
        )
    options[OTHER_OPTION] = (
        "The fault is carried by an object that is not a listed workload: an admission webhook, a "
        "ResourceQuota or LimitRange, an RBAC role, a node, or a namespace-wide policy. See `cluster.findings`."
    )
    return options


def build_component_questions(snapshot: ClusterSnapshot) -> dict[str, Choice | Noul]:
    """Request 1: component selection, fault object kind, and fault visibility, all over the same state."""
    return {
        COMPONENT_QUESTION: Choice(
            instructions={
                "question": "Exactly one fault was injected into this Kubernetes application after it was "
                "deployed and healthy. Which single component is the origin of that fault?",
                "what_the_state_contains": [
                    "Each entry in `components` has `signals`: abnormalities computed from the live cluster "
                    "about that component's own spec, configuration, policies, jobs, pods, and events. "
                    "`healthy: true` means no abnormality at all.",
                    "`cluster.recent_changes` lists objects modified after they were created, or created after the "
                    "workloads they act on, with `seconds_before_symptoms` relative to `cluster.symptom_onset` (the "
                    "start of the earliest abnormality that is still active). `nearest_change_before_symptoms: true` "
                    "marks the component of the change that most closely preceded it (`most_recent_change: true` "
                    "when no onset is known).",
                    "A signal starting with `errors logged by ... other component(s) ... name this component` "
                    "means other components fail when calling this one; `errors_point_to` on a component lists "
                    "the components its own errors name.",
                    "`log_error_lines`, `log_signals`, and `log_findings` describe a component's own error "
                    "logs, with warm-up lines already removed. Each log signal has `state`: `ongoing` (still "
                    "occurring) or `stopped` (silent for much longer than its usual gap); stopped errors are history "
                    "and count toward nothing else. `telemetry_error_lines` counts failures to export "
                    "traces, metrics, or logs to the telemetry pipeline; they do not break application requests "
                    "and never identify the origin. `startup_history` and `startup_events` are settled deploy-time "
                    "churn.",
                    "`cluster.findings` lists non-workload objects with a computed signal (webhooks, quotas, "
                    "policies, nodes, objects written after deploy). `role: observability` marks telemetry "
                    "components (dashboards, tracing, metrics, log shipping).",
                    "`evidence_kinds` on each component summarises its signals: `change` (its own spec, Service, "
                    "Secret, or ConfigMap was written after deploy), `configuration` (an abnormal setting of its "
                    "own), `pointed_at` (other components' errors name it), `own_failure_logs` (its logs show "
                    "authentication, authorization, or data-processing failures), `symptoms` (pod, replica, or "
                    "resource state only), `error_logs_only`.",
                ],
                "triage_order": [
                    "1. Prefer a component with `change` in `evidence_kinds` when the change is consistent with "
                    "the abnormal signals in the application. A change on a telemetry component "
                    "(`role: observability`) counts only when application components' errors name that component; "
                    "operators and the platform write to telemetry objects routinely.",
                    "2. Otherwise prefer a component with `configuration` in `evidence_kinds`: a policy selecting "
                    "it, a Service selecting the wrong pods, a traffic policy, hostAliases, a non-default "
                    "dnsPolicy, a probe or env-var or mount mismatch, a rollout strategy, a stuck job, a quota "
                    "rejecting its pods.",
                    "3. Otherwise prefer a component with `pointed_at` or `own_failure_logs` in `evidence_kinds`.",
                    "4. `symptoms` (Pending, terminating, not ready, restarting, at a CPU or memory limit) and "
                    "`error_logs_only` never select a component on their own. A component whose "
                    "`evidence_kinds` is only [`symptoms`] or [`error_logs_only`] is a candidate only if no "
                    "component in the application has `change`, `configuration`, `pointed_at`, or "
                    "`own_failure_logs`, and `cluster.findings` is empty.",
                    "5. A component whose only abnormality is error logs about calls to another component is a "
                    "victim of that component. A telemetry component (`role: observability`) is a low-priority "
                    "candidate unless rule 1, 2, or 3 selects it.",
                    f"6. Select `{OTHER_OPTION}` when `cluster.findings` contains a webhook, quota, RBAC, or node "
                    "object whose signal explains the unhealthy components better than any component's own "
                    "spec does (for example pods rejected by a quota or by a webhook with no backend).",
                ],
            },
            criteria=component_options(snapshot),
        ),
        FAULT_KIND_QUESTION: Choice(
            instructions={
                "question": "Which kind of Kubernetes object carries the injected change that explains the "
                "abnormal signals in `components` and `cluster.findings`?",
                "how_to_decide": [
                    "Judge the object that was changed, not the object that shows symptoms: pods rejected by a "
                    "ResourceQuota mean `namespace_policy`; a Service that now selects the wrong pods means "
                    "`service`; a Deployment whose env or dnsPolicy changed means `workload_spec`.",
                    "Pick `not_visible` only if nothing in the state identifies a changed or abnormal object.",
                ],
            },
            criteria=FAULT_OBJECT_KINDS,
        ),
        FAULT_VISIBLE_QUESTION: Noul(
            instructions="Does the state contain clear evidence of an active fault: at least one component with "
            "`healthy: false` whose `signals` describe a failure, an entry in `cluster.findings`, a firing alert "
            "in `cluster.firing_alerts`, or a node with problems in `cluster.nodes`?",
            criteria={
                "true": "There is at least one concrete failure signal in the state.",
                "false": "Every component is healthy and there are no findings, alerts, or node problems.",
            },
        ),
    }


STATE_SYMPTOM_MARKERS = (
    "is terminating",
    "pod phase Pending",
    "is running but not ready",
    "desired replicas are ready",
)


def build_evidence(component: dict) -> list[dict]:
    """Concrete evidence lines for one component, ids E1..En. Configuration and change first, state last."""
    signals = list(component.get("signals", []))
    config_like = [s for s in signals if not any(m in s for m in STATE_SYMPTOM_MARKERS) and "is terminating" not in s]
    state_like = [s for s in signals if s not in config_like and "is terminating" not in s]
    items: list[str] = config_like
    items += component.get("spec_flags", [])
    for ev in component.get("warning_events", []):
        items.append(f"event {ev.get('reason')} x{ev.get('count')} on {ev.get('object')}: {ev.get('message')}")
    for f in component.get("log_findings", []) or []:
        klass = ", ".join(f.get("classes") or ["error"])
        target = f" naming {', '.join(f['targets'])}" if f.get("targets") else ""
        items.append(f"log finding ({klass}{target}, x{f.get('count')}): {f.get('line')}")
    for sig in component.get("log_signals", []):
        items.append(f"log x{sig.get('count')}: {sig.get('line')}")
    for cname, lines in (component.get("previous_container_logs") or {}).items():
        for line in lines[-3:]:
            items.append(f"previous log of container {cname}: {line}")
    items += state_like
    for c in component.get("containers", []):
        limits = c.get("limits") or {}
        probes = c.get("probes") or {}
        extra = f" probes {probes}" if probes else ""
        if limits:
            items.append(f"container {c.get('name')} image {c.get('image')} limits {limits}{extra}")
        else:
            items.append(f"container {c.get('name')} image {c.get('image')} (no resource limits){extra}")
    unique = list(dict.fromkeys(items))[:MAX_EVIDENCE_ITEMS]
    return [{"id": f"E{i + 1}", "text": text} for i, text in enumerate(unique)]


def build_characterization_questions(evidence: list[dict]) -> dict[str, Choice]:
    """Request 2: fault category (a cause) and the single most revealing evidence item."""
    questions: dict[str, Choice] = {
        CATEGORY_QUESTION: Choice(
            instructions={
                "question": "Which category names the cause of the fault in `component`?",
                "how_to_decide": [
                    "Pods Pending, not ready, restarting, or terminating are symptoms. Choose the category of "
                    "what made them so: a probe pointing at the wrong port is health_probe_config, a quota "
                    "rejection is admission_or_namespace_policy, a nodeSelector no node matches is "
                    "scheduling_constraint, a NetworkPolicy is network_policy.",
                    "Judge from `component.signals`, `component.spec_flags`, `component.warning_events`, "
                    "`component.log_findings`, `component.previous_container_logs`, and `component.containers`.",
                ],
            },
            criteria=FAULT_CATEGORIES,
        )
    }
    if evidence:
        questions[EVIDENCE_QUESTION] = Choice(
            instructions={
                "question": "Which single item in `evidence` most directly reveals the mechanism of the fault "
                "in `component`?",
                "how_to_decide": [
                    "Prefer an item that names a configuration, policy, object, or change (a duplicated env "
                    "var, a NetworkPolicy, a quota rejection message, a probe target, a modified Secret) over "
                    "an item that only describes a state (Pending, terminating, not ready, restarts, replicas).",
                    "Prefer an item with a specific value or object name over a generic one.",
                    "Each option id names one item in `evidence`.",
                ],
            },
            criteria={item["id"]: item["text"] for item in evidence},
        )
    return questions


# --------------------------------------------------------------------------- client


def make_client(model: str | None = None) -> TypeSafeClient:
    """A TypeSafe client tuned for large states: longer HTTP timeout, a few retries."""
    return TypeSafeClient(
        model=model or jev_model(),
        timeout=float(os.getenv("JEV_DIAG_HTTP_TIMEOUT", "90")),
        retry=RetryPolicy(max_retries=3, backoff_max=10.0, timeout=240.0),
    )


class JevDiagnoser:
    def __init__(
        self,
        client: SystemOneClient | None = None,
        *,
        state_token_budget: int = DEFAULT_STATE_TOKEN_BUDGET,
        characterize: bool = True,
        trace: DecisionTrace | None = None,
    ) -> None:
        self._client = client
        self.state_token_budget = state_token_budget
        self.characterize = characterize
        self.trace = trace
        self.requests: list[dict] = []  # request/response artifacts for the logs dir

    def _trace(self, phase: str, event: str, **payload: Any) -> None:
        if self.trace is not None:
            self.trace.record(phase, event, **payload)

    @property
    def client(self) -> SystemOneClient:
        if self._client is None:
            self._client = make_client()
        return self._client

    def _ask(self, label: str, state: dict, questions: dict) -> Any:
        tokens = estimate_tokens(state)
        wire_questions = {k: _question_to_dict(q) for k, q in questions.items()}
        logger.info("Jev request %s: %d questions, ~%d state tokens", label, len(questions), tokens)
        self._trace(
            "jev",
            "request",
            label=label,
            state_tokens=tokens,
            questions=wire_questions,
            option_counts={k: len(q.get("criteria") or {}) for k, q in wire_questions.items() if isinstance(q, dict)},
            state=state,
        )
        started = time.monotonic()
        try:
            response = self.client.system_one(state, questions)
        except BaseException as exc:
            self._trace(
                "jev",
                "response",
                label=label,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        entry = {
            "label": label,
            "state": state,
            "questions": wire_questions,
            "model": getattr(response, "model", None),
            "latency_ms": latency_ms,
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            },
            "answers": {k: _answer_to_dict(a) for k, a in response.answers.items()},
        }
        self.requests.append(entry)
        self._trace(
            "jev",
            "response",
            label=label,
            model=entry["model"],
            latency_ms=latency_ms,
            usage=entry["usage"],
            answers=entry["answers"],
        )
        return response

    def diagnose(self, snapshot: ClusterSnapshot) -> Diagnosis:
        if not snapshot.components:
            raise ValueError("No workloads found in the application namespaces; nothing to classify.")

        full_state = snapshot.to_state()
        state, trims = fit_state_to_budget(full_state, self.state_token_budget)
        self._trace(
            "jev",
            "budget_fit",
            label="select_component",
            budget=self.state_token_budget,
            tokens_before=estimate_tokens(full_state),
            tokens_after=estimate_tokens(state),
            applied=trims,
        )
        response = self._ask("select_component", state, build_component_questions(snapshot))
        component_result = ChoiceResult.from_answer(response.choices[COMPONENT_QUESTION])
        fault_kind_result = (
            ChoiceResult.from_answer(response.choices[FAULT_KIND_QUESTION])
            if FAULT_KIND_QUESTION in response.choices
            else None
        )
        fault_visible = (
            response.nouls[FAULT_VISIBLE_QUESTION].noul if FAULT_VISIBLE_QUESTION in response.nouls else None
        )
        chosen = component_result.choice
        ranked = component_result.ranked()
        runner_up = next(((cid, p) for cid, p in ranked if cid != chosen), None)
        fault_object = select_fault_object(snapshot, chosen, fault_kind_result)
        self._trace(
            "decide",
            "component",
            chosen=chosen,
            is_other=chosen == OTHER_OPTION,
            confidence=component_result.confidence,
            probability=component_result.probabilities.get(chosen),
            fault_visible=None if fault_visible is None else round(float(fault_visible), 4),
            ranked=ranked,
            runner_up=runner_up,
            low_confidence=component_result.confidence < LOW_CONFIDENCE,
            runner_up_flagged=bool(
                component_result.confidence < LOW_CONFIDENCE and runner_up and runner_up[1] >= RUNNER_UP_MIN_PROBABILITY
            ),
            fault_kind=fault_kind_result.choice if fault_kind_result else None,
            fault_kind_ranked=fault_kind_result.ranked()[:4] if fault_kind_result else [],
            fault_object=fault_object,
            chosen_signals=snapshot.components.get(chosen, {}).get("signals", []),
            chosen_log_error_lines=snapshot.components.get(chosen, {}).get("log_error_lines"),
        )

        category_result = evidence_result = None
        evidence: list[dict] = []
        detail_trims: list[str] = []
        if self.characterize and chosen in snapshot.components:
            component = snapshot.components[chosen]
            evidence = build_evidence(component)
            self._trace("decide", "evidence", component=chosen, evidence=evidence)
            detail_state, detail_trims = fit_state_to_budget(
                {
                    "application": snapshot.app,
                    "component": component,
                    "evidence": evidence,
                    "related_alerts": [
                        a
                        for a in snapshot.cluster.get("firing_alerts", [])
                        if a["alertname"] in component.get("alerts", [])
                    ],
                    "cluster_findings": snapshot.cluster.get("findings") or [],
                    "nodes_with_problems": [n for n in snapshot.cluster.get("nodes", []) if n.get("problems")],
                    "components": {},
                },
                DETAIL_STATE_TOKEN_BUDGET,
            )
            detail_state.pop("components")
            self._trace(
                "jev",
                "budget_fit",
                label="characterize_fault",
                budget=DETAIL_STATE_TOKEN_BUDGET,
                tokens_before=None,
                tokens_after=estimate_tokens(detail_state),
                applied=detail_trims,
            )
            response2 = self._ask("characterize_fault", detail_state, build_characterization_questions(evidence))
            category_result = ChoiceResult.from_answer(response2.choices[CATEGORY_QUESTION])
            if EVIDENCE_QUESTION in response2.choices:
                evidence_result = ChoiceResult.from_answer(response2.choices[EVIDENCE_QUESTION])
            key_text = None
            if evidence_result is not None:
                key_text = next((e["text"] for e in evidence if e["id"] == evidence_result.choice), None)
            self._trace(
                "decide",
                "characterization",
                component=chosen,
                category=category_result.choice,
                category_confidence=category_result.confidence,
                category_ranked=category_result.ranked(),
                key_evidence_id=evidence_result.choice if evidence_result else None,
                key_evidence_probability=(
                    evidence_result.probabilities.get(evidence_result.choice) if evidence_result else None
                ),
                key_evidence_confidence=evidence_result.confidence if evidence_result else None,
                key_evidence_text=key_text,
                evidence_ranked=evidence_result.ranked()[:5] if evidence_result else [],
            )

        input_tokens = _sum_usage("input_tokens", self.requests)
        output_tokens = _sum_usage("output_tokens", self.requests)
        diagnosis = Diagnosis(
            component=chosen,
            component_result=component_result,
            fault_visible=None if fault_visible is None else round(float(fault_visible), 4),
            category_result=category_result,
            evidence_result=evidence_result,
            evidence=evidence,
            model=str(self.requests[-1].get("model") or ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            fault_kind_result=fault_kind_result,
            fault_object=fault_object,
            trims={"select_component": trims, "characterize_fault": detail_trims},
        )
        diagnosis.text = build_diagnosis_text(snapshot, diagnosis)
        self._trace("decide", "diagnosis_text", component=chosen, text=diagnosis.text, chars=len(diagnosis.text))
        return diagnosis


# Fault-object kinds and the finding kinds that can carry them.
FAULT_OBJECT_FINDING_KINDS: dict[str, tuple[str, ...]] = {
    "admission_webhook": ("MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"),
    "namespace_policy": ("ResourceQuota", "LimitRange"),
    "admission_or_namespace": (
        "MutatingWebhookConfiguration",
        "ValidatingWebhookConfiguration",
        "ResourceQuota",
        "LimitRange",
    ),
    "network_policy": ("NetworkPolicy",),
    "node_or_cluster": ("Node", "ConfigMap"),
    "dns": ("ConfigMap",),
    "service": ("Service",),
    "config_object": ("Secret", "ConfigMap"),
    "rbac": ("ClusterRole", "Role", "ClusterRoleBinding", "RoleBinding"),
}
# Objects that carry a fault for the workloads they act on; the diagnosis leads with them.
CARRIER_KINDS = {
    "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration",
    "ResourceQuota",
    "LimitRange",
    "ClusterRole",
    "Role",
    "ClusterRoleBinding",
    "RoleBinding",
    "NetworkPolicy",
}


def finding_linked(snapshot: ClusterSnapshot, finding: dict, cid: str) -> bool:
    """True when the finding's object acts on the component, or the component uses the object."""
    if cid in (finding.get("components") or []):
        return True
    comp = snapshot.components.get(cid) or {}
    kind, name = finding.get("kind") or "", finding.get("name")
    refs = comp.get("config_refs") or {}
    if kind.lower() == "secret" and name in (refs.get("secrets") or []):
        return True
    if kind.lower() == "configmap" and name in (refs.get("configmaps") or []):
        return True
    if kind == "Service" and any(s.get("name") == name for s in comp.get("services") or []):
        return True
    if kind == "NetworkPolicy" and any(p.get("name") == name for p in comp.get("network_policies") or []):
        return True
    return (
        bool(comp.get("name"))
        and re.search(rf"(?<![a-z0-9-]){re.escape(comp['name'])}(?![a-z0-9-])", finding.get("signal") or "") is not None
    )


def select_fault_object(
    snapshot: ClusterSnapshot, chosen: str, kind_result: ChoiceResult | None, *, require_link: bool = False
) -> dict | None:
    """Name the non-workload object that carries the fault, from `cluster.findings`, when the kind says so.

    For a component conclusion, objects linked to the component are preferred; with `require_link` an object
    that does not act on the component (and is not used by it) is never named.
    """
    findings = snapshot.cluster.get("findings") or []
    if not findings:
        return None
    kind = kind_result.choice if kind_result else None
    wanted = FAULT_OBJECT_FINDING_KINDS.get(kind or "", ())
    candidates = [f for f in findings if f.get("kind") in wanted]
    if kind == "dns":
        candidates = [f for f in candidates if f.get("name") == "coredns"]
    if chosen in snapshot.components:
        linked = [f for f in candidates if finding_linked(snapshot, f, chosen)]
        if linked or require_link:
            candidates = linked
    if not candidates and chosen == OTHER_OPTION:
        candidates = findings  # anything the collector flagged is better than nothing
    if not candidates:
        return None

    def score(f: dict) -> tuple:
        sig = f.get("signal") or ""
        return (
            chosen in snapshot.components and finding_linked(snapshot, f, chosen),
            any(
                m in sig
                for m in (
                    "not granted",
                    "rejects",
                    "rewrites pods of",
                    "0 ready endpoints",
                    "overrides name resolution",
                )
            ),
            "modified" in sig or "created" in sig,
        )

    return max(candidates, key=score)


def _sum_usage(key: str, requests: list[dict]) -> int | None:
    values = [r["usage"].get(key) for r in requests]
    known = [v for v in values if isinstance(v, int)]
    return sum(known) if known else None


def _question_to_dict(question: Any) -> Any:
    try:
        import msgspec

        return msgspec.to_builtins(question)
    except Exception:  # noqa: BLE001 - artifacts are best effort
        return str(question)


def _answer_to_dict(answer: Any) -> Any:
    try:
        import msgspec

        return msgspec.to_builtins(answer)
    except Exception:  # noqa: BLE001
        return str(answer)


# --------------------------------------------------------------------------- diagnosis text


def _blocked_input_text(blocked: dict) -> str:
    if blocked.get("source") == "broker":
        return (
            f"consumer group {blocked.get('group')} is stalled at offset {blocked.get('offset')} of topic "
            f"{blocked.get('topic')} partition {blocked.get('partition')}: the record at that offset is not being "
            "processed while newer records arrive"
        )
    text = f"one input item fails processing repeatedly and blocks later input: {blocked.get('evidence')}"
    if blocked.get("input"):
        text += f"; the input comes from {blocked['input']}"
    return text


def _affected_by_object(snapshot: ClusterSnapshot, fo: dict) -> list[str]:
    """Workloads a cluster-level object acts on: those it is known to act on, and those whose signals or current
    warning events name it (a FailedCreate event naming a webhook or quota, for example), with their shortfall."""
    names = {fo.get("name")} | {
        wh.get("webhook")
        for wh in snapshot.cluster.get("admission_webhooks") or []
        if wh.get("configuration") == fo.get("name")
    }
    names = {n for n in names if n}
    linked = set(fo.get("components") or [])
    out = []
    for cid, c in snapshot.components.items():
        if c.get("role") == "observability":
            continue
        mentions = [s for s in c.get("signals") or [] if any(n in s for n in names)]
        events = [e for e in c.get("warning_events") or [] if any(n in (e.get("message") or "") for n in names)]
        if cid not in linked and not mentions and not events:
            continue
        if mentions:
            detail = mentions[0]
        elif events:
            detail = f"{events[0].get('reason')} x{events[0].get('count')} on {events[0].get('object')}: {events[0].get('message')}"
        else:
            detail = (c.get("signals") or ["acted on by this object"])[0]
        shortfall = next((s for s in c.get("signals") or [] if "desired replicas are ready" in s), None)
        out.append(
            f"{c['kind'].lower()}/{c['name']}: {detail}"
            + (f"; {shortfall}" if shortfall and shortfall != detail else "")
        )
    return out


def _impact_lines(snapshot: ClusterSnapshot, cid: str) -> list[str]:
    """How the root-cause component affects others: still-occurring errors that name it, clients left without an
    endpoint, and objects stuck waiting on it."""
    comp = snapshot.components.get(cid) or {}
    lines = []
    for src, classes in (comp.get("referenced_by_errors_from") or {}).items():
        summary = ", ".join(f"{k} x{v}" for k, v in classes.items())
        lines.append(f"{src} fails when calling this component ({summary}, still occurring)")
    for svc in comp.get("services") or []:
        for gap in svc.get("local_policy_gaps") or []:
            lines.append(f"client {gap} gets no endpoint of Service {svc['name']}")
    for obj in snapshot.cluster.get("stuck_terminating") or []:
        if cid in (obj.get("components") or []):
            lines.append(
                f"{obj['kind']} {obj['name']} has been terminating for {obj.get('terminating_for_s')}s, held by "
                f"finalizer {', '.join(obj.get('finalizers') or [])}"
                + (f"; {obj['permission_gap']}" if obj.get("permission_gap") else "")
            )
    return lines[:6]


def build_diagnosis_text(snapshot: ClusterSnapshot, d: Diagnosis) -> str:
    """Assemble the submitted diagnosis from typed answers and collected evidence. No generation."""
    comp = snapshot.components.get(d.component)
    ranked = d.component_result.ranked()
    runner_up = next(((cid, p) for cid, p in ranked if cid != d.component), None)
    lines: list[str] = []
    fo = d.fault_object

    if comp is None:
        if fo:
            ns = f" in namespace `{fo['namespace']}`" if fo.get("namespace") else ""
            lines.append(f"Root cause object: {fo['kind']} `{fo['name']}`{ns}.")
            lines.append(f"Mechanism: {fo['signal']}.")
        else:
            lines.append(
                "Root cause: the fault does not originate in any listed workload of "
                f"{snapshot.app.get('name')} (namespaces {', '.join(snapshot.app.get('namespaces', []))})."
            )
        others = [
            f
            for f in (snapshot.cluster.get("findings") or [])
            if f is not fo and not (fo and f.get("kind") == fo.get("kind") and f.get("name") == fo.get("name"))
        ][:3]
        if others:
            lines.append("Other cluster-level findings:")
            lines += [f"- {f['kind']} {f['name']}: {f['signal']}" for f in others]
        affected = _affected_by_object(snapshot, fo) if fo else []
        if affected:
            lines.append("Impact on workloads:")
            lines += [f"- {a}" for a in affected[:4]]
    else:
        key = None
        if d.evidence_result is not None:
            key = next((e["text"] for e in d.evidence if e["id"] == d.evidence_result.choice), None)
        where = f"{comp['kind']} `{comp['name']}` in namespace `{comp['namespace']}`"
        carrier = fo if fo and fo.get("kind") in CARRIER_KINDS else None
        if carrier:
            ns = f" in namespace `{carrier['namespace']}`" if carrier.get("namespace") else ""
            lines.append(f"Root cause object: {carrier['kind']} `{carrier['name']}`{ns}, acting on {where}.")
            lines.append(f"What the object does: {carrier['signal']}.")
        elif d.blocked_input:
            lines.append(f"Root cause: {_blocked_input_text(d.blocked_input)}. The blocked component is {where}.")
        else:
            lines.append(f"Root cause component: {where}.")
        if key:
            lines.append(f"Mechanism: {key}.")
        if fo and not carrier and fo.get("kind") in ("Service", "Secret", "ConfigMap", "secret", "configmap"):
            lines.append(f"Fault object: {fo['kind']} `{fo['name']}`: {fo['signal']}.")
        if d.category_result is not None:
            cat = d.category_result.choice
            if cat == "admission_or_namespace_policy" and fo and fo.get("kind") in ("ResourceQuota", "LimitRange"):
                lines.append(
                    f"Fault type: namespace policy ({fo['kind']} `{fo['name']}` rejects or alters this workload's pods)."
                )
            elif cat == "admission_or_namespace_policy" and fo and "WebhookConfiguration" in (fo.get("kind") or ""):
                lines.append(
                    f"Fault type: admission webhook ({fo['kind']} `{fo['name']}` rejects or rewrites this workload's pods)."
                )
            else:
                lines.append(f"Fault type: {cat.replace('_', ' ')} ({CATEGORY_GLOSS.get(cat, cat)}).")
        # Deterministic mismatches code found in the component's own spec (tree mode). They are facts, not
        # model choices, so every one is reported: a fault can have more than one mechanism.
        confirmed_step = next(
            (
                s
                for s in ((d.investigation or {}).get("steps") or [])
                if s.get("component") == d.component and s.get("node") == "investigate"
            ),
            None,
        )
        checks = [
            x for x in (confirmed_step or {}).get("spec_checks") or [] if x != key and "no ready endpoints" not in x
        ]
        if checks:
            lines.append("Configuration mismatches found by code:")
            lines += [f"- {x}" for x in checks[:4]]
        state = [s for s in comp.get("signals", []) if not s.startswith("errors logged by") and s != key]
        if state or comp.get("log_error_lines"):
            lines.append("Abnormal state observed on this component:")
            lines += [f"- {s}" for s in state[:12]]
            if comp.get("log_error_lines"):
                lines.append(f"- {comp['log_error_lines']} error-like log lines still occurring in the recent pod logs")
        impact = _impact_lines(snapshot, d.component)
        if impact:
            lines.append("Impact on other components:")
            lines += [f"- {x}" for x in impact]
        if comp.get("spec_flags"):
            lines.append("Notable spec settings:")
            lines += [f"- {s}" for s in comp["spec_flags"][:6]]
        for c in comp.get("containers", []):
            limits = c.get("limits") or {}
            lines.append(
                f"- container {c.get('name')}: image {c.get('image')}" + (f", limits {limits}" if limits else "")
            )
        if comp.get("warning_events"):
            lines.append("Warning events:")
            lines += [
                f"- {e['reason']} x{e['count']} on {e['object']}: {e['message']}" for e in comp["warning_events"][:4]
            ]
        current = [s for s in comp.get("log_signals") or [] if s.get("state") != "stopped"]
        if current:
            lines.append("Error-like log lines still occurring:")
            lines += [f"- x{s['count']}: {s['line']}" for s in current[:4]]
        for cname, prev in (comp.get("previous_container_logs") or {}).items():
            lines.append(f"Last lines of the previous (crashed) run of container {cname}:")
            lines += [f"- {ln}" for ln in prev[-4:]]
        if comp.get("alerts"):
            lines.append(f"Firing alerts on this component: {', '.join(dict.fromkeys(comp['alerts']))}.")

    if d.investigation:
        steps = d.investigation.get("steps") or []
        confirmed = next(
            (s for s in steps if s.get("component") == d.component and s.get("node") == "investigate"), None
        )
        fallback = d.investigation.get("fallback")
        if confirmed and not fallback:
            lines.append(
                f"Classification: Jev ({d.model or 'jev'}) confirmed this origin at investigation step "
                f"{confirmed['step']} with probability {confirmed['origin_p']:.2f} "
                f"(triage probability {d.component_result.probabilities.get(d.component, 0):.2f})."
            )
        elif confirmed:
            lines.append(
                f"Classification: Jev ({d.model or 'jev'}) selected this origin as the most likely candidate after "
                f"{len(steps)} investigation step(s) (origin probability {confirmed['origin_p']:.2f}, triage "
                f"probability {d.component_result.probabilities.get(d.component, 0):.2f})."
            )
        else:
            lines.append(
                f"Classification: Jev ({d.model or 'jev'}) selected this origin with triage probability "
                f"{d.component_result.probabilities.get(d.component, 0):.2f} after {len(steps)} investigation step(s)."
            )
    else:
        lines.append(
            f"Classification: Jev ({d.model or 'jev'}) selected this origin with probability "
            f"{d.component_result.probabilities.get(d.component, 0):.2f} (confidence {d.component_result.confidence:.2f})."
        )
    if d.component_result.confidence < LOW_CONFIDENCE and runner_up and runner_up[1] >= RUNNER_UP_MIN_PROBABILITY:
        lines.append(f"Low confidence: `{runner_up[0]}` (probability {runner_up[1]:.2f}) cannot be ruled out.")
    if d.fault_visible is not None and d.fault_visible < 0.5:
        lines.append("Note: the collected state shows weak evidence of any active fault.")
    return "\n".join(lines)
