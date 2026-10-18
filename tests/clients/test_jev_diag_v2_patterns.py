"""jev_diag v2 explanation builders on small hand-made snapshots."""

from types import SimpleNamespace

from clients.jev_diag.v2.explain import PARTIAL, SUPPORTS, UNKNOWN
from clients.jev_diag.v2.patterns import build_explanations


def comp(kind, name, *, healthy=True, **extra):
    return {"kind": kind, "name": name, "namespace": "shop", "healthy": healthy, "signals": [], **extra}


def snapshot(components, **cluster):
    return SimpleNamespace(components=components, cluster={"symptom_onset": "2026-10-18T10:00:00+00:00", **cluster})


def test_each_field_changed_in_one_revision_gets_its_own_explanation_plus_a_joint_one():
    proxy = comp(
        "Deployment",
        "edge-proxy",
        healthy=False,
        template_changes={
            "revision": "3",
            "changed_at": "2026-10-18T09:58:00+00:00",
            "changes": [
                "container envoy env FILTER_RULE added: rule-v2",
                "container envoy limits.cpu: unset -> 100m",
            ],
        },
    )
    keys = {e.key: e for e in build_explanations(snapshot({"deployment/edge-proxy": proxy}))}
    regex = keys["change:deployment/edge-proxy:container envoy env FILTER_RULE added: rule-v2"]
    assert regex.values == ["container envoy env FILTER_RULE added: rule-v2"]
    assert regex.origin == "deployment/shop/edge-proxy" and regex.predictions[0].status == SUPPORTS
    assert "change:deployment/edge-proxy:container envoy limits.cpu: unset -> 100m" in keys
    assert len(keys["change:deployment/edge-proxy:together"].values) == 2
    assert keys["partial:deployment/edge-proxy"].pattern == PARTIAL


def test_a_change_made_after_the_estimated_onset_is_not_held_against_it():
    late = comp(
        "Deployment",
        "cart",
        healthy=False,
        template_changes={"revision": "2", "changed_at": "2026-10-18T10:05:00+00:00", "changes": ["image changed"]},
    )
    (change,) = [e for e in build_explanations(snapshot({"deployment/cart": late})) if e.pattern == "change"]
    assert change.predictions[0].status == UNKNOWN and not change.contradicted()


def test_errors_naming_a_healthy_server_make_a_dependency_explanation_with_a_probe_check():
    components = {
        "deployment/frontend": comp("Deployment", "frontend", healthy=False, errors_point_to=["deployment/rate"]),
        "deployment/search": comp("Deployment", "search", healthy=False, errors_point_to=["deployment/rate"]),
        "deployment/rate": comp(
            "Deployment", "rate", referenced_by_errors_from={"deployment/frontend": {}, "deployment/search": {}}
        ),
    }
    dep = next(e for e in build_explanations(snapshot(components)) if e.pattern == "dependency")
    assert dep.origin == "deployment/shop/rate"
    assert dep.impact == ["deployment/shop/frontend", "deployment/shop/search"]
    own = dep.predictions[1]
    assert own.status == UNKNOWN and own.check == "probe"


def test_recorded_object_changes_and_findings_become_acts_on_explanations():
    components = {"deployment/recommendation": comp("Deployment", "recommendation", healthy=False)}
    change = {
        "object": "networkpolicy/block-all",
        "kind": "NetworkPolicy",
        "name": "block-all",
        "namespace": "shop",
        "at": "2026-10-18T09:59:00+00:00",
        "affects": ["deployment/recommendation"],
    }
    finding = {
        "kind": "ResourceQuota",
        "name": "mem",
        "namespace": "shop",
        "signal": "exceeded quota",
        "components": [],
    }
    found = build_explanations(snapshot(components, _changes=[change], findings=[finding]))
    netpol = next(e for e in found if e.origin == "networkpolicy/shop/block-all")
    assert netpol.pattern == "acts-on" and netpol.impact == ["deployment/shop/recommendation"]
    assert any(e.origin == "resourcequota/shop/mem" and e.values == ["exceeded quota"] for e in found)


def test_timeouts_on_a_saturated_callee_make_an_interaction_explanation_with_both_sides_settings():
    components = {
        "deployment/search": comp("Deployment", "search", healthy=False, errors_point_to=["deployment/rate"]),
        "deployment/rate": comp(
            "Deployment",
            "rate",
            healthy=False,
            signals=["CPU at its limit"],
            referenced_by_errors_from={"deployment/search": {"connectivity or timeout": 40}},
        ),
    }
    details = {
        "deployment/search": {"workload": {"containers": [{"env": [{"name": "RATE_RPC_MAX_ATTEMPTS", "value": "5"}]}]}},
        "deployment/rate": {"workload": {"containers": [{"env": [{"name": "RATE_BACKEND_QPS_LIMIT", "value": "20"}]}]}},
    }
    found = {e.key: e for e in build_explanations(snapshot(components), details)}
    loop = found["amplify:deployment/search->deployment/rate"]
    assert loop.origin == "interaction/shop/search--rate"
    assert loop.values == ["deployment/search RATE_RPC_MAX_ATTEMPTS=5", "deployment/rate RATE_BACKEND_QPS_LIMIT=20"]
    assert loop.predictions[1].status == SUPPORTS


def test_auth_failures_and_repeated_input_failures_make_hidden_state_and_bad_input_explanations():
    components = {
        "deployment/cart": comp("Deployment", "cart", healthy=False, errors_point_to=["deployment/cache"]),
        "deployment/cache": comp(
            "Deployment", "cache", referenced_by_errors_from={"deployment/cart": {"authentication failure": 9}}
        ),
        "deployment/fraud": comp("Deployment", "fraud", healthy=False),
    }
    details = {
        "deployment/fraud": {
            "logs": {"repeated_failures": [{"line": "cannot decode order at offset 20"}]},
            "input_candidates": [{"env": "QUEUE_TOPIC", "value": "events"}, {"env": "GROUP", "value": "fraud"}],
        }
    }
    found = {e.key: e for e in build_explanations(snapshot(components), details)}
    hidden = found["hidden-state:deployment/cache"]
    assert hidden.origin == "deployment/shop/cache" and hidden.impact == ["deployment/shop/cart"]
    assert {e.origin for e in found.values() if e.pattern == "bad-input"} == {"input/shop/events", "input/shop/fraud"}


def test_a_wrong_setting_of_its_own_explains_a_broken_component_unless_another_object_already_does():
    pvc = "PVC data is ReadWriteOnce (attachable to one node at a time) but 2 replicas of this Deployment mount it"
    tracing = comp("Deployment", "tracer", healthy=False, role="observability", signals=["firing alert X", pvc])
    selected = comp("Deployment", "cart", healthy=False, signals=["selected by NetworkPolicy block-all (denies all)"])
    policy = {
        "kind": "NetworkPolicy",
        "name": "block-all",
        "namespace": "shop",
        "signal": "denies all",
        "components": ["deployment/cart"],
    }
    keys = {
        e.key: e
        for e in build_explanations(
            snapshot({"deployment/tracer": tracing, "deployment/cart": selected}, findings=[policy])
        )
    }
    own = keys["own-setting:deployment/tracer"]
    assert own.values == [pvc] and pvc in own.mechanism and own.predictions[0].status == SUPPORTS
    assert "partial:deployment/tracer" not in keys  # one candidate per origin
    assert "own-setting:deployment/cart" not in keys and "partial:deployment/cart" in keys  # the policy explains it
