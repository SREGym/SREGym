"""Jev (TypeSafe System One) questions for root-cause component classification.

Code owns the workflow: the collector decides what evidence exists, this module
asks Jev a few narrow, typed judgments and turns the typed answers into a
diagnosis text deterministically.

Request 1 (all components, in parallel): which component is the origin of the
fault; what kind of Kubernetes object carries the change; whether any
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

from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient

from clients.jev_diag import decisions_client
from clients.jev_diag.config import jev_model
from clients.jev_diag.trace import DecisionTrace
from clients.jev_diag.v2.collector import (  # noqa: F401 - re-exported
    ClusterSnapshot,
    estimate_tokens,
    fit_state_to_budget,
)

logger = logging.getLogger("all.jev_diag.v2.classifier")

# Jev 1.13 limits: 64k tokens for state + all questions, 32k for state + longest question.
DEFAULT_STATE_TOKEN_BUDGET = int(os.getenv("JEV_DIAG_STATE_TOKEN_BUDGET", "24000"))
DETAIL_STATE_TOKEN_BUDGET = 20000
MAX_EVIDENCE_ITEMS = 24
LOW_CONFIDENCE = 0.3
RUNNER_UP_MIN_PROBABILITY = 0.25

OTHER_OPTION = "other"
COMPONENT_QUESTION = "root_cause_component"
FAULT_KIND_QUESTION = "fault_object_kind"
CATEGORY_QUESTION = "fault_category"
EVIDENCE_QUESTION = "key_evidence"

# What kind of Kubernetes object carries the change. Cluster-level kinds are the ones a
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
    "data_or_traffic": "Not a Kubernetes object: the application's data or inputs, its load, or settings it "
    "holds outside Kubernetes objects.",
    "not_visible": "The collected state shows no change or abnormality that identifies the object.",
}
CLUSTER_LEVEL_KINDS = {"namespace_policy", "admission_webhook", "rbac", "node_or_cluster", "network_policy"}

# Cause-oriented categories. Pods Pending, not ready, restarting, or terminating are symptoms; each
# category names the thing that made them so.
FAULT_CATEGORIES: dict[str, dict[str, Any]] = {
    "config_value": {
        "what": "A wrong, missing, or conflicting value in the component's own spec or configuration: environment, "
        "command and arguments, the address or port of a dependency, configuration content, or a setting that "
        "changes how much work it does or how much it consumes.",
        "not_for": "Any fault a more specific category describes: credentials (credentials_or_auth), probes "
        "(health_probe_config), resources (resource_limits), DNS settings (dns_resolution), a Service object's "
        "selector or ports (service_routing), rollout settings, volumes.",
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
        "what": "A readiness, liveness, or startup probe points at the wrong path, port, or command, or its "
        "timing (delay, period, timeout, failure threshold) is too strict for the container, so healthy "
        "containers are reported not ready or are restarted.",
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
        "what": "Volume problems of any kind (persistent, emptyDir, memory-backed): a claim Pending or Lost, "
        "conflicting or wrong mounts, mount failures, a full disk or volume.",
        "not_for": "ConfigMap or Secret mounts with wrong content (config_value).",
    },
    "job_lifecycle": {
        "what": "A Job or CronJob does not complete or runs wrongly: a container that never exits, overlapping or "
        "missed runs, wrong restartPolicy, backoffLimit, or schedule.",
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
        "what": "The component's inputs are the problem: input it cannot process, or more work than it can serve "
        "(load, amplified retries, expensive requests).",
        "not_for": "Failures explained by the component's own spec or a Kubernetes object.",
    },
    "code_bug": {
        "what": "The component runs with correct configuration, resources, and inputs, and still fails "
        "because of its own logic.",
        "not_for": "Anything explained by another category.",
    },
    "missing_object": {
        "what": "An object the application needs was deleted or never created: a Service its clients address, a "
        "ConfigMap, Secret, or PersistentVolumeClaim it references.",
        "not_for": "An object that exists with a wrong value (config_value, service_routing).",
    },
    "certificate_or_tls": {
        "what": "A TLS certificate is expired, not yet valid, issued for other names, or not trusted by its "
        "clients (an Ingress, a webhook, or a server presenting it).",
        "not_for": "Credentials such as passwords or tokens (credentials_or_auth).",
    },
    "pod_network_or_ipam": {
        "what": "The cluster network fails the pods: the network plugin cannot set up pod networking, IP addresses "
        "are exhausted, routing between nodes is broken.",
        "not_for": "A NetworkPolicy (network_policy) or a Service's routing (service_routing).",
    },
    "preemption_or_priority": {
        "what": "Pods are preempted or kept from scheduling by pods of higher priority (a PriorityClass, a default "
        "priority, a workload that takes the capacity).",
        "not_for": "Placement rules of the pod itself (scheduling_constraint).",
    },
    "node_or_kubelet": {
        "what": "A node or its kubelet fails the pods: resource pressure and evictions, kubelet or container "
        "runtime settings, a node that is not ready.",
        "not_for": "Limits set too low for one workload (resource_limits).",
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
    second: Diagnosis | None = None  # a second, independent cause, when the investigation found one

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
        "The fault is carried by an object that is not a listed workload, of any kind, that writes, admits, "
        "schedules, routes, preempts, or constrains the workloads (for example an admission webhook, a quota, a "
        "role, a node, a scheduling priority, or a network or namespace-wide policy). See `cluster.findings`."
    )
    return options


def build_component_questions(snapshot: ClusterSnapshot) -> dict[str, Choice]:
    """Request 1: component selection and fault object kind, over the same state."""
    return {
        COMPONENT_QUESTION: Choice(
            instructions={
                "question": "This Kubernetes application is failing. Which component is the origin of the fault, "
                "where it started?",
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
                    "the abnormal signals in the application. Ask who made the change: a value written into a "
                    "component by another workload, a scheduled job, or a controller points at that writer. A "
                    "change on a telemetry component "
                    "(`role: observability`) counts only when application components' errors name that component; "
                    "operators and the platform write to telemetry objects routinely.",
                    "2. Otherwise prefer a component with `configuration` in `evidence_kinds`: a setting of its own, "
                    "or of an object that acts on it, that code found inconsistent or abnormal.",
                    "3. Otherwise prefer a component with `pointed_at` or `own_failure_logs` in `evidence_kinds`.",
                    "4. `symptoms` (Pending, terminating, not ready, restarting, at a CPU or memory limit) and "
                    "`error_logs_only` are weaker evidence, but not nothing: some faults change no object. When no "
                    "change or configuration "
                    "explains the abnormal signals, prefer the component whose own failure started first and "
                    "explains the others' failures over a component whose only problem is calling it. A change "
                    "or finding that does not explain the observed failures does not outrank it.",
                    "5. A component whose only abnormality is error logs about calls to another component is a "
                    "victim of that component. A telemetry component (`role: observability`) is a low-priority "
                    "candidate unless rule 1, 2, or 3 selects it.",
                    f"6. Select `{OTHER_OPTION}` when `cluster.findings` contains an object outside the "
                    "application's workloads, of any kind, that writes, admits, schedules, routes, preempts, or "
                    "constrains them, and whose signal explains the unhealthy components better than any "
                    "component's own spec does.",
                ],
            },
            criteria=component_options(snapshot),
        ),
        FAULT_KIND_QUESTION: Choice(
            instructions={
                "question": "Which kind of Kubernetes object carries the change or abnormality that explains the "
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
    }


STATE_SYMPTOM_MARKERS = (
    "is terminating",
    "pod phase Pending",
    "is running but not ready",
    "desired replicas are ready",
)


# --------------------------------------------------------------------------- client


def make_client(model: str | None = None) -> SystemOneClient:
    """A TypeSafe client tuned for large states: longer HTTP timeout, a few retries."""
    if decisions_client.enabled():
        return decisions_client.make_client(timeout=float(os.getenv("JEV_DIAG_HTTP_TIMEOUT", "90")))
    return TypeSafeClient(
        model=model or jev_model(),
        timeout=float(os.getenv("JEV_DIAG_HTTP_TIMEOUT", "90")),
        retry=RetryPolicy(max_retries=3, backoff_max=10.0, timeout=240.0),
    )


KEEP_OPTIONS = (OTHER_OPTION, "none", "both")


def fit_choices(questions: dict, limit: int | None, priority: list[str]) -> dict:
    """Every choice question as decision models accept it: at least two options (a one-option question gets
    "none of these"), and at most `limit` when the model has one, keeping the `priority` options first and the
    other/none/both options always."""
    out = {}
    for name, q in questions.items():
        criteria = getattr(q, "criteria", None)
        if not isinstance(q, Choice) or criteria is None:
            out[name] = q
            continue
        criteria = dict(criteria)
        if len(criteria) < 2:
            criteria.setdefault("none", "None of these.")
        if limit and len(criteria) > limit:
            keep = [k for k in criteria if k in KEEP_OPTIONS]
            rank = {k: i for i, k in enumerate(priority)}
            rest = sorted((k for k in criteria if k not in KEEP_OPTIONS), key=lambda k: rank.get(k, len(rank)))
            chosen = set(rest[: max(0, limit - len(keep))]) | set(keep)
            criteria = {k: v for k, v in criteria.items() if k in chosen}
        out[name] = q if criteria == dict(q.criteria) else Choice(instructions=q.instructions, criteria=criteria)
    return out


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
        self.max_choice_options: int | None = None  # a decision model's limit on options per question, if any
        self.option_priority: list[str] = []  # options kept first when a question must be cut to that limit

    def _trace(self, phase: str, event: str, **payload: Any) -> None:
        if self.trace is not None:
            self.trace.record(phase, event, **payload)

    @property
    def client(self) -> SystemOneClient:
        if self._client is None:
            self._client = make_client()
        return self._client

    def _ask(self, label: str, state: dict, questions: dict) -> Any:
        questions = fit_choices(questions, self.max_choice_options, self.option_priority)
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
