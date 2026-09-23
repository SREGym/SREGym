"""Unit tests for the Jev diagnosis agent: deterministic collection, budget fitting, question shape, text."""

from __future__ import annotations

from dataclasses import dataclass

import msgspec
import pytest
from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer

from clients.jev_diag import classifier, collector
from clients.jev_diag.classifier import (
    CATEGORY_QUESTION,
    COMPONENT_QUESTION,
    EVIDENCE_QUESTION,
    FAULT_CATEGORIES,
    FAULT_VISIBLE_QUESTION,
    OTHER_OPTION,
    JevDiagnoser,
    build_characterization_questions,
    build_component_questions,
    build_evidence,
)
from clients.jev_diag.collector import (
    ClusterSnapshot,
    attach_network_policies,
    attach_services,
    attach_warning_events,
    component_for_pod,
    estimate_tokens,
    extract_log_signals,
    fit_state_to_budget,
    parse_alerts,
    parse_cpu_millis,
    parse_memory_bytes,
    selector_matches,
    summarize_pod,
    summarize_workload,
)

NS = "astronomy-shop"


def deployment(name: str, *, replicas: int = 1, ready: int = 1, labels: dict | None = None, **template_spec) -> dict:
    labels = labels or {"app": name}
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NS},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": f"example/{name}:1.0",
                            "env": [{"name": "PORT", "value": "8080"}],
                            "resources": {"limits": {"cpu": "100m", "memory": "128Mi"}},
                        }
                    ],
                    **template_spec,
                },
            },
        },
        "status": {"readyReplicas": ready, "availableReplicas": ready, "updatedReplicas": replicas},
    }


def crashing_pod(name: str, owner_rs: str, labels: dict) -> dict:
    return {
        "metadata": {
            "name": name,
            "namespace": NS,
            "labels": labels,
            "ownerReferences": [{"kind": "ReplicaSet", "name": owner_rs}],
        },
        "spec": {"nodeName": "kind-worker", "containers": [{"name": "frontend"}]},
        "status": {
            "phase": "Running",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "ContainersNotReady",
                    "message": "containers with unready status: [frontend]",
                }
            ],
            "containerStatuses": [
                {
                    "name": "frontend",
                    "ready": False,
                    "restartCount": 4,
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 1m20s restarting failed container",
                        }
                    },
                    "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                }
            ],
        },
    }


def make_components(*objs: dict) -> dict[str, dict]:
    comps = {}
    for obj in objs:
        c = summarize_workload(obj)
        comps[collector.component_id(c["kind"], c["name"])] = c
    return comps


# --------------------------------------------------------------------------- collector


def test_selector_matching_is_subset_and_empty_selector_matches_nothing():
    assert selector_matches({"app": "a"}, {"app": "a", "tier": "web"})
    assert not selector_matches({"app": "a", "tier": "db"}, {"app": "a"})
    assert not selector_matches({}, {"app": "a"})
    assert not selector_matches(None, {"app": "a"})


def test_quantity_parsing_keeps_arithmetic_in_code():
    assert parse_cpu_millis("100m") == 100
    assert parse_cpu_millis("1") == 1000
    assert parse_cpu_millis("0.5") == 500
    assert parse_memory_bytes("128Mi") == 128 * 1024**2
    assert parse_memory_bytes("1G") == 1e9
    assert parse_cpu_millis(None) is None and parse_memory_bytes("bogus") is None


def test_summarize_pod_reads_crashloop_signals():
    pod = summarize_pod(crashing_pod("frontend-7d9f-abc12", "frontend-7d9f", {"app": "frontend"}))
    assert pod["ready"] == "0/1"
    assert pod["restarts"] == 4
    assert "container frontend waiting in CrashLoopBackOff" in pod["signals"]
    assert "container frontend restarted 4 times" in pod["signals"]
    assert any("last terminated Error exit=1" in s for s in pod["container_states"])
    assert pod["conditions"] == ["Ready=False (ContainersNotReady): containers with unready status: [frontend]"]


def test_pod_maps_to_deployment_via_replicaset_owner_then_selector():
    comps = make_components(deployment("frontend"), deployment("checkout"))
    pod = summarize_pod(crashing_pod("frontend-7d9f-abc12", "frontend-7d9f", {"app": "frontend"}))
    pod["_namespace"] = NS
    assert component_for_pod(pod, comps) == "deployment/frontend"
    orphan = summarize_pod(
        {"metadata": {"name": "x", "labels": {"app": "checkout"}}, "spec": {}, "status": {"phase": "Running"}}
    )
    orphan["_namespace"] = NS
    assert component_for_pod(orphan, comps) == "deployment/checkout"


def test_workload_summary_flags_host_aliases_and_replica_shortfall():
    comp = summarize_workload(
        deployment("frontend", replicas=2, ready=0, hostAliases=[{"ip": "10.0.0.9", "hostnames": ["cart"]}])
    )
    assert comp["spec_flags"] == ["pod spec sets hostAliases: 10.0.0.9 -> cart"]
    assert collector.replica_signals(comp) == ["only 0 of 2 desired replicas are ready"]
    assert collector.replica_signals(summarize_workload(deployment("x", replicas=0, ready=0))) == [
        "scaled to zero replicas (desired=0)"
    ]


def test_service_without_endpoints_and_dangling_selector_are_signals():
    comps = make_components(deployment("frontend"), deployment("cart"))
    services = [
        {
            "metadata": {"name": "frontend", "namespace": NS},
            "spec": {"selector": {"app": "frontend"}, "ports": [{"port": 80, "targetPort": 8080}]},
        },
        {
            "metadata": {"name": "cart", "namespace": NS},
            "spec": {"selector": {"app": "cartt"}, "ports": [{"port": 80, "targetPort": 8080}]},
        },
    ]
    endpoints = [{"metadata": {"name": "frontend", "namespace": NS}, "subsets": []}]
    dangling = attach_services(services, endpoints, comps)
    assert (
        "service frontend selects this component but has 0 ready endpoints" in comps["deployment/frontend"]["signals"]
    )
    assert [d["name"] for d in dangling] == ["cart"]
    assert any("does not match this workload's pod labels" in s for s in comps["deployment/cart"]["signals"])


def test_network_policy_deny_all_ingress_attaches_to_selected_component():
    comps = make_components(deployment("frontend"), deployment("cart"))
    policies = [
        {
            "metadata": {"name": "block-cart", "namespace": NS},
            "spec": {"podSelector": {"matchLabels": {"app": "cart"}}, "policyTypes": ["Ingress"]},
        }
    ]
    attach_network_policies(policies, comps)
    assert comps["deployment/cart"]["signals"] == ["selected by NetworkPolicy block-cart (denies all ingress)"]
    assert comps["deployment/frontend"]["signals"] == []


def test_warning_events_attach_by_pod_name_prefix_and_summarise_reasons():
    comps = make_components(deployment("frontend"))
    events = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "count": 12,
            "lastTimestamp": "2026-09-17T10:00:00Z",
            "message": "Back-off restarting failed container",
            "involvedObject": {"kind": "Pod", "name": "frontend-7d9f-abc12", "namespace": NS},
        },
        {
            "type": "Normal",
            "reason": "Pulled",
            "count": 1,
            "message": "ok",
            "involvedObject": {"kind": "Pod", "name": "frontend-7d9f-abc12", "namespace": NS},
        },
        {
            "type": "Warning",
            "reason": "FailedMount",
            "count": 1,
            "lastTimestamp": "2026-09-17T09:00:00Z",
            "message": "x",
            "involvedObject": {"kind": "Pod", "name": "ghost-1", "namespace": NS},
        },
    ]
    unassigned: list[dict] = []
    attach_warning_events(events, comps, unassigned)
    assert comps["deployment/frontend"]["warning_events"][0]["reason"] == "BackOff"
    assert comps["deployment/frontend"]["signals"] == ["warning events: BackOff x12"]
    assert [e["object"] for e in unassigned] == ["pod/ghost-1"]


def test_log_signals_dedupe_normalise_and_skip_info_level():
    raw = [
        "[pod/frontend-1/frontend] 2026-09-17T10:00:01.123Z error: dial tcp 10.1.2.3:6379: connection refused",
        "[pod/frontend-1/frontend] 2026-09-17T10:00:02.456Z error: dial tcp 10.1.2.3:6379: connection refused",
        '[pod/frontend-1/frontend] level=info ts=2026-09-17T10:00:03Z msg="request timeout configured"',
        "[pod/frontend-1/frontend] INFO handled 0 errors",
        "[pod/frontend-1/frontend] all good",
    ]
    signals = extract_log_signals(raw)
    assert len(signals) == 1
    assert signals[0]["count"] == 2
    assert signals[0]["line"].startswith("frontend: error: dial tcp 10.1.2.3:6379: connection refused")


def test_parse_alerts_accepts_mcp_literal_and_no_alerts_text():
    assert parse_alerts("No firing alerts") == []
    raw = str(
        [
            {
                "labels": {
                    "alertname": "HighErrorRate",
                    "namespace": NS,
                    "service_name": "checkout",
                    "severity": "critical",
                },
                "annotations": {"summary": "checkout 5xx > 5%"},
                "activeAt": "t",
            }
        ]
    )
    alerts = parse_alerts(raw)
    assert alerts[0]["alertname"] == "HighErrorRate"
    comps = make_components(deployment("checkout"), deployment("frontend"))
    collector.attach_alerts(alerts, comps)
    assert comps["deployment/checkout"]["signals"] == ["firing alert HighErrorRate"]
    assert comps["deployment/frontend"]["signals"] == []


def test_resource_usage_flags_cpu_at_limit():
    comps = make_components(deployment("frontend"))
    collector.attach_resource_usage("frontend-1 frontend 97m 40Mi\n", comps, {"frontend-1": "deployment/frontend"})
    assert any("cpu usage 97m is at its limit 100m" in s for s in comps["deployment/frontend"]["signals"])


def test_compact_drops_empty_fields_but_keeps_false_booleans():
    assert collector.compact({"a": None, "b": [], "c": {}, "d": False, "e": [{"f": None, "g": 1}]}) == {
        "d": False,
        "e": [{"g": 1}],
    }


# --------------------------------------------------------------------------- budget


def big_snapshot(n_components: int = 30) -> ClusterSnapshot:
    comps = {}
    for i in range(n_components):
        comp = summarize_workload(deployment(f"svc{i}"))
        comp["healthy"] = i % 5 != 0
        comp["signals"] = [] if comp["healthy"] else [f"pod svc{i}-1: container svc{i} waiting in CrashLoopBackOff"]
        comp["log_signals"] = [{"count": k, "line": f"svc{i}: error number {k} " + "x" * 150} for k in range(8)]
        comp["warning_events"] = [
            {"reason": "BackOff", "count": k, "object": f"pod/svc{i}-1", "message": "m" * 200} for k in range(6)
        ]
        comp["pods"] = [{"name": f"svc{i}-{k}", "phase": "Running", "ready": "1/1", "restarts": 0} for k in range(4)]
        comps[f"deployment/svc{i}"] = comp
    return ClusterSnapshot(
        app={"name": "app", "namespaces": [NS]}, components=comps, cluster={"nodes": [], "firing_alerts": []}
    )


def test_fit_state_to_budget_trims_until_it_fits_and_keeps_original_intact():
    snap = big_snapshot()
    state = snap.to_state()
    before = estimate_tokens(state)
    fitted, applied = fit_state_to_budget(state, before // 4)
    assert estimate_tokens(fitted) <= before // 4
    assert applied  # something had to be trimmed
    assert estimate_tokens(state) == before  # deep copy: original untouched
    healthy = fitted["components"]["deployment/svc1"]
    unhealthy = fitted["components"]["deployment/svc0"]
    assert unhealthy["signals"]  # signals on unhealthy components survive every trim step
    assert "pods" not in healthy or len(healthy["pods"]) <= len(unhealthy.get("pods", []))


def test_fit_state_to_budget_is_a_no_op_when_small():
    snap = big_snapshot(2)
    fitted, applied = fit_state_to_budget(snap.to_state(), 10**6)
    assert applied == []
    assert fitted == snap.to_state()


# --------------------------------------------------------------------------- questions


def small_snapshot() -> ClusterSnapshot:
    comps = make_components(deployment("frontend", ready=0), deployment("cart"), deployment("checkout"))
    comps["deployment/frontend"]["signals"] = [
        "only 0 of 1 desired replicas are ready",
        "container frontend waiting in CrashLoopBackOff",
    ]
    comps["deployment/frontend"]["warning_events"] = [
        {"reason": "BackOff", "count": 9, "object": "pod/frontend-1", "message": "Back-off restarting failed container"}
    ]
    comps["deployment/frontend"]["previous_container_logs"] = {
        "frontend": ["panic: required env var CART_ADDR is not set"]
    }
    comps["deployment/checkout"]["signals"] = ["3 error-like log lines in the last 200 lines per pod"]
    comps["deployment/checkout"]["log_signals"] = [{"count": 3, "line": "checkout: rpc error: frontend unavailable"}]
    for c in comps.values():
        c["healthy"] = not c["signals"]
    return ClusterSnapshot(
        app={"name": "shop", "namespaces": [NS], "description": "d"},
        components=comps,
        cluster={"nodes": [], "firing_alerts": []},
    )


def test_component_question_offers_every_component_plus_other_and_serialises():
    snap = small_snapshot()
    questions = build_component_questions(snap)
    choice = questions[COMPONENT_QUESTION]
    assert isinstance(choice, Choice)
    assert set(choice.criteria) == set(snap.components) | {OTHER_OPTION}
    assert isinstance(questions[FAULT_VISIBLE_QUESTION], Noul)
    wire = msgspec.to_builtins(choice)
    assert wire["type"] == "choice"
    assert "components" in msgspec.json.encode(wire["instructions"]).decode()
    # the option descriptions point at the matching state entry
    assert 'components["deployment/frontend"]' in choice.criteria["deployment/frontend"]


def test_evidence_and_characterisation_questions():
    snap = small_snapshot()
    evidence = build_evidence(snap.components["deployment/frontend"])
    ids = [e["id"] for e in evidence]
    assert ids == [f"E{i + 1}" for i in range(len(ids))]
    texts = [e["text"] for e in evidence]
    assert "container frontend waiting in CrashLoopBackOff" in texts
    assert any("CART_ADDR" in t for t in texts)
    questions = build_characterization_questions(evidence)
    assert set(questions[CATEGORY_QUESTION].criteria) == set(FAULT_CATEGORIES)
    assert set(questions[EVIDENCE_QUESTION].criteria) == set(ids)
    assert EVIDENCE_QUESTION not in build_characterization_questions([])


# --------------------------------------------------------------------------- diagnoser with a fake client


@dataclass
class FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class FakeResponse:
    answers: dict
    model: str = "jev-1.13.0"
    usage: FakeUsage = None  # type: ignore[assignment]

    @property
    def choices(self):
        return {k: v for k, v in self.answers.items() if isinstance(v, ChoiceAnswer)}

    @property
    def nouls(self):
        return {k: v for k, v in self.answers.items() if isinstance(v, NoulAnswer)}


class FakeClient:
    def __init__(self, scripted: list[FakeResponse]):
        self.scripted = list(scripted)
        self.calls: list[tuple[dict, dict]] = []

    def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        return self.scripted.pop(0)


def choice_answer(probs: dict[str, float], confidence: float) -> ChoiceAnswer:
    best = max(probs, key=probs.get)
    return ChoiceAnswer(choice=best, confidence=confidence, probabilities=probs)


def test_diagnose_runs_two_requests_and_builds_text_from_typed_answers():
    snap = small_snapshot()
    evidence = build_evidence(snap.components["deployment/frontend"])
    key_id = next(e["id"] for e in evidence if "CART_ADDR" in e["text"])
    client = FakeClient(
        [
            FakeResponse(
                {
                    COMPONENT_QUESTION: choice_answer(
                        {
                            "deployment/frontend": 0.8,
                            "deployment/checkout": 0.15,
                            "deployment/cart": 0.05,
                            OTHER_OPTION: 0.0,
                        },
                        0.7,
                    ),
                    FAULT_VISIBLE_QUESTION: NoulAnswer(noul=0.97),
                },
                usage=FakeUsage(1200, 40),
            ),
            FakeResponse(
                {
                    CATEGORY_QUESTION: choice_answer(
                        {
                            **{k: 0.0 for k in FAULT_CATEGORIES},
                            "config_value": 0.9,
                            "image_or_startup_failure": 0.1,
                        },
                        0.85,
                    ),
                    EVIDENCE_QUESTION: choice_answer(
                        {e["id"]: (0.9 if e["id"] == key_id else 0.1 / (len(evidence) - 1)) for e in evidence}, 0.8
                    ),
                },
                usage=FakeUsage(600, 30),
            ),
        ]
    )
    diagnosis = JevDiagnoser(client, state_token_budget=10**6).diagnose(snap)

    assert diagnosis.component == "deployment/frontend"
    assert diagnosis.category_result.choice == "config_value"
    assert diagnosis.input_tokens == 1800 and diagnosis.output_tokens == 70
    assert diagnosis.model == "jev-1.13.0"

    first_state, first_questions = client.calls[0]
    assert set(first_state["components"]) == set(snap.components)
    assert set(first_questions) == {COMPONENT_QUESTION, FAULT_VISIBLE_QUESTION, "fault_object_kind"}
    second_state, second_questions = client.calls[1]
    assert second_state["component"]["name"] == "frontend" and "components" not in second_state
    assert set(second_questions) == {CATEGORY_QUESTION, EVIDENCE_QUESTION}

    text = diagnosis.text
    assert text.startswith("Root cause component: Deployment `frontend` in namespace `astronomy-shop`.")
    assert "Fault type: config value (" in text
    assert "Mechanism: previous log of container frontend: panic: required env var CART_ADDR is not set." in text
    assert "Low confidence" not in text
    assert len(JevDiagnoser(client).requests) == 0  # artifacts are per-instance


def test_low_confidence_and_other_paths_are_reported():
    snap = small_snapshot()
    snap.cluster["network_policies"] = [{"name": "deny-all", "namespace": NS, "effect": "denies all ingress"}]
    client = FakeClient(
        [
            FakeResponse(
                {
                    COMPONENT_QUESTION: choice_answer(
                        {
                            OTHER_OPTION: 0.45,
                            "deployment/frontend": 0.4,
                            "deployment/cart": 0.1,
                            "deployment/checkout": 0.05,
                        },
                        0.1,
                    ),
                    FAULT_VISIBLE_QUESTION: NoulAnswer(noul=0.3),
                },
                usage=FakeUsage(1000, 10),
            )
        ]
    )
    diagnosis = JevDiagnoser(client, state_token_budget=10**6).diagnose(snap)
    assert diagnosis.component == OTHER_OPTION
    assert diagnosis.category_result is None  # no second request for `other`
    assert len(client.calls) == 1
    assert "does not originate in any listed workload" in diagnosis.text
    assert "Low confidence: `deployment/frontend` (probability 0.40) cannot be ruled out." in diagnosis.text
    assert "weak evidence of any active fault" in diagnosis.text


def test_diagnose_rejects_empty_snapshot():
    snap = ClusterSnapshot(app={}, components={}, cluster={})
    with pytest.raises(ValueError):
        JevDiagnoser(FakeClient([])).diagnose(snap)


def test_state_budget_default_is_under_jev_limits():
    assert classifier.DEFAULT_STATE_TOKEN_BUDGET < 32000
    assert classifier.DETAIL_STATE_TOKEN_BUDGET < 32000
