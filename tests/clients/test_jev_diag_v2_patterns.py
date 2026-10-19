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
    assert "partial:deployment/tracer" not in keys  # observability: no bare partial
    assert "own-setting:deployment/cart" not in keys and "partial:deployment/cart" in keys  # the policy explains it
    hang = "init container hang has been running for 130s; the pod's main containers cannot start until it exits"
    restarted = comp(
        "Deployment",
        "web",
        healthy=False,
        signals=[f"pod web-1: {hang}", f"pod web-2: {hang}"],
        template_changes={"revision": "3", "changed_at": "2026-10-18T09:58:00+00:00", "changes": ["annotation added"]},
    )
    keys = {e.key: e for e in build_explanations(snapshot({"deployment/web": restarted}))}
    assert keys["own-setting:deployment/web"].values == [hang]  # its own change does not hide it; replicas count once


def test_a_reader_still_using_values_from_before_a_rewrite_is_the_origin_and_the_object_contributes():
    stale = (
        "Secret db-conn was rewritten at 2026-10-18T15:53:29Z, after 1 of 1 running containers of this workload "
        "started; it is read through env DB_URL, which are fixed when the container starts, so those containers "
        "still use the previous values"
    )
    reader = comp(
        "Deployment", "catalog", healthy=False, signals=[stale, "server deployment/db rejects logins for account u"]
    )
    (e,) = [e for e in build_explanations(snapshot({"deployment/catalog": reader})) if e.key.startswith("stale-read")]
    assert e.origin == "deployment/shop/catalog" and e.contributing == ["secret/shop/db-conn"]
    assert e.specific_groups() == 1  # the rejected logins, beyond its own symptoms


def test_a_finding_that_names_an_outside_object_is_that_objects_evidence_not_the_workloads_own():
    wrong = "service frontend selector {'app': 'front'} does not match this workload's pod labels {'app': 'frontend'}"
    web = comp(
        "Deployment",
        "frontend",
        healthy=False,
        signals=[wrong, "service frontend selects this component but has 0 ready endpoints"],
    )
    svc = {"kind": "Service", "name": "frontend", "namespace": "shop", "signal": "selector matches no workload"}
    keys = {e.key: e for e in build_explanations(snapshot({"deployment/frontend": web}, findings=[svc]))}
    assert "own-setting:deployment/frontend" not in keys
    (e,) = [e for e in keys.values() if e.pattern == "acts-on" and e.origin == "service/shop/frontend"]
    assert e.impact == ["deployment/shop/frontend"] and e.specific_groups() == 1  # linked by the check


def test_a_caller_that_fails_because_a_component_it_is_configured_to_call_is_broken_is_a_victim():
    oom = "pod v-1: container cache was OOMKilled"
    components = {
        "deployment/frontend": comp("Deployment", "frontend", healthy=False, errors_point_to=["deployment/cart"]),
        "deployment/cart": comp(
            "Deployment",
            "cart",
            healthy=False,
            evidence_kinds=["pointed_at", "symptoms"],
            referenced_by_errors_from={"deployment/frontend": {}},
        ),
        "deployment/cache": comp("Deployment", "cache", healthy=False, evidence_kinds=["symptoms"], signals=[oom]),
    }
    details = {
        "deployment/cart": {"workload": {"containers": [{"env": [{"name": "CACHE_ADDR", "value": "cache:6379"}]}]}}
    }
    keys = {e.key: e for e in build_explanations(snapshot(components), details)}
    assert keys["dependency:deployment/cart"].contradicted() and keys["partial:deployment/cart"].contradicted()
    cache = keys["dependency:deployment/cache"]
    assert not cache.contradicted() and cache.specific_groups() == 1 and oom in cache.values


def test_a_component_that_appeared_after_the_application_and_just_before_the_symptoms_is_a_candidate():
    at = "2026-10-18T09:59:00+00:00"
    components = {f"deployment/{n}": comp("Deployment", n, created=at) for n in ("a", "b", "c")}
    components["job/flood"] = comp("Job", "flood", healthy=False, created="2026-10-18T09:59:40+00:00")
    keys = {e.key for e in build_explanations(snapshot(components))}
    assert "arrival:job/flood" in keys and not any(k.startswith("arrival:deployment/") for k in keys)


def test_a_caller_whose_own_errors_name_a_crashing_component_is_its_victim():
    err = {"line": "url-store: <error> Failed to create mongodb index: connection refused calling 'url-store-mong…"}
    noise = {"line": "url-store: [OTLP Exporter] Export() failed: DNS resolution failed for otel-collector:4317"}
    components = {
        "deployment/url-store": comp(
            "Deployment",
            "url-store",
            healthy=False,
            evidence_kinds=["pointed_at"],
            log_findings=[err, noise],
            referenced_by_errors_from={"deployment/text": {}},
        ),
        "deployment/text": comp("Deployment", "text", healthy=False, errors_point_to=["deployment/url-store"]),
        "deployment/url-store-mongodb": comp(
            "Deployment",
            "url-store-mongodb",
            healthy=False,
            evidence_kinds=["symptoms"],
            signals=["container mongo was OOMKilled"],
        ),
        "deployment/otel-collector": comp(
            "Deployment", "otel-collector", healthy=False, role="observability", evidence_kinds=["symptoms"]
        ),
    }
    keys = {e.key: e for e in build_explanations(snapshot(components))}
    assert keys["dependency:deployment/url-store"].contradicted()  # the caller is the victim
    db = keys["dependency:deployment/url-store-mongodb"]
    assert not db.contradicted() and db.specific_groups() == 1
    assert "dependency:deployment/otel-collector" not in keys  # exporter noise links nothing


def test_store_links_ignore_stopped_errors_env_order_and_keep_every_caller():
    cache = comp("Deployment", "cache", healthy=False, evidence_kinds=["symptoms"], signals=["container was OOMKilled"])
    stopped = {"logs": {"error_samples": [{"line": "failed to connect to redis", "state": "stopped"}]}}
    api = comp("Deployment", "api", healthy=False)
    keys = {
        e.key
        for e in build_explanations(
            snapshot(
                {
                    "deployment/api": api,
                    "deployment/redis": comp("Deployment", "redis", healthy=False, evidence_kinds=["symptoms"]),
                }
            ),
            {"deployment/api": stopped},
        )
    }
    assert "dependency:deployment/redis" not in keys  # a stopped error links nothing
    env = [{"name": "CACHE_HOST", "value": "cache"}, {"name": "PORT", "value": "6379"}]
    details = {cid: {"workload": {"containers": [{"env": env}]}} for cid in ("deployment/a", "deployment/b")}
    comps = {
        "deployment/a": comp("Deployment", "a", healthy=False),
        "deployment/b": comp("Deployment", "b", healthy=False),
        "deployment/cache": cache,
    }
    (dep,) = [e for e in build_explanations(snapshot(comps), details) if e.key == "dependency:deployment/cache"]
    assert dep.impact == ["deployment/shop/a", "deployment/shop/b", "deployment/shop/cache"]  # both callers kept
