"""jev_diag v2: evidence that must not be lost or misread (collection, change records, evidence kinds)."""

from __future__ import annotations

from clients.jev_diag.timeutil import parse_time
from clients.jev_diag.v2 import changes, checks, collector, derive, investigate


def test_telemetry_names_are_whole_tokens():
    assert derive.OBSERVABILITY_RE.search("otel-collector")
    assert derive.OBSERVABILITY_RE.search("prometheus-server")
    assert not derive.OBSERVABILITY_RE.search("hotel-reservation")
    assert not derive.OBSERVABILITY_RE.search("temporal-worker")


def test_a_late_change_in_a_long_env_value_is_reported_where_it_differs():
    base = "jdbc:postgresql://db:5432/orders?ssl=true&connectTimeout=10&socketTimeout=30&applicationName=orders"
    old = {"containers": [{"name": "app", "env": [{"name": "DSN", "value": base}]}]}
    new = {"containers": [{"name": "app", "env": [{"name": "DSN", "value": base.replace("orders", "billing")}]}]}
    diff = changes.template_diff({"spec": old}, {"spec": new})
    assert any("env DSN:" in d and "billing" in d for d in diff), diff


def test_a_scale_by_an_operator_is_a_change_and_by_an_autoscaler_is_not():
    meta = {
        "managedFields": [
            {"manager": "kubectl", "subresource": "scale", "time": "2026-10-19T08:00:00Z"},
            {"manager": "kube-controller-manager", "subresource": "scale", "time": "2026-10-19T08:01:00Z"},
            {"manager": "kubelet", "subresource": "status", "time": "2026-10-19T08:02:00Z"},
        ]
    }
    assert [w["manager"] for w in changes.spec_writes(meta)] == ["kubectl"]


def test_no_rollout_deadlock_is_claimed_for_zero_replicas():
    def workload(replicas: int) -> dict:
        return {
            "kind": "Deployment",
            "metadata": {"name": "x", "namespace": "ns"},
            "spec": {
                "replicas": replicas,
                "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "25%", "maxSurge": "25%"}},
                "selector": {"matchLabels": {"app": "x"}},
                "template": {
                    "metadata": {"labels": {"app": "x"}},
                    "spec": {"containers": [{"name": "x", "image": "i"}]},
                },
            },
            "status": {},
        }

    for replicas in (0, 1, 4):  # 25% surge rounds up, so a rollout always progresses
        signals = collector.summarize_workload(workload(replicas))["signals"]
        assert not any("can never progress" in s for s in signals), (replicas, signals)


def test_error_lines_include_resource_exhaustion_and_certificate_failures():
    for line in (
        "java.lang.OutOfMemoryError: Java heap space",
        "listen tcp :8080: bind: address already in use",
        "write /data/x: no space left on device",
        "accept4: too many open files",
        "Get https://cart: x509: certificate has expired",
    ):
        assert collector.is_error_line(line), line


def test_a_crash_keeps_its_message_when_a_stack_trace_follows():
    lines = ["starting", "panic: no reachable servers", "", *[f"\tmain.go:{i}" for i in range(30)], "exit status 2"]
    excerpt = collector.crash_excerpt([ln for ln in lines if ln], 12)
    assert excerpt[-1] == "earlier: panic: no reachable servers"
    assert len(excerpt) == 12


def test_an_investigation_sees_the_warning_events_attributed_to_the_workload():
    comp = {
        "warning_events": [
            {
                "reason": "FailedCreate",
                "object": "replicaset/web-7d9f",
                "message": 'pods "web-7d9f-x" is forbidden: violates PodSecurity "restricted:latest"',
                "last_seen": "2026-10-19T08:05:00Z",
                "count": 9,
            }
        ]
    }
    events = investigate.merged_events([], comp)
    assert events[0]["reason"] == "FailedCreate" and events[0]["type"] == "Warning"


def test_preemption_reported_as_a_normal_event_reaches_the_workload():
    comps = {
        "deployment/web": {"kind": "Deployment", "name": "web", "namespace": "ns", "signals": [], "warning_events": []}
    }
    preempted = {
        "type": "Normal",
        "reason": "Preempted",
        "message": "Preempted by pod 1234 on node worker2",
        "involvedObject": {"kind": "Pod", "name": "web-7d9f6c8b5-abcde", "namespace": "ns"},
        "lastTimestamp": "2026-10-19T08:05:00Z",
    }
    collector.attach_warning_events([preempted], comps, [])
    assert comps["deployment/web"]["signals"][0].startswith("pods of this workload were preempted")


def test_symptom_onset_ignores_what_predates_the_application_and_foreign_alerts():
    comps = {
        "deployment/web": {
            "namespace": "ns",
            "created": "2026-10-19T08:00:00Z",
            "warning_events": [{"reason": "BackOff", "first_seen": "2026-10-19T08:06:00Z"}],
        }
    }
    cluster = {
        "firing_alerts": [
            {"alertname": "Old", "active_since": "2026-10-19T07:00:00Z", "labels": {"namespace": "ns"}},
            {"alertname": "Elsewhere", "active_since": "2026-10-19T08:02:00Z", "labels": {"namespace": "other"}},
        ]
    }
    moment, source = changes.estimate_symptom_onset(comps, cluster, parse_time("2026-10-19T08:10:00Z"))
    assert source == "BackOff events on deployment/web" and moment == parse_time("2026-10-19T08:06:00Z")


def test_a_network_policy_selecting_by_expression_selects_its_pods():
    policy = {
        "metadata": {"name": "deny", "namespace": "ns"},
        "spec": {
            "podSelector": {"matchExpressions": [{"key": "app", "operator": "In", "values": ["web"]}]},
            "policyTypes": ["Ingress"],
        },
    }
    comps = {
        "deployment/web": {"namespace": "ns", "template_labels": {"app": "web"}, "network_policies": [], "signals": []},
        "deployment/db": {"namespace": "ns", "template_labels": {"app": "db"}, "network_policies": [], "signals": []},
    }
    summaries = collector.attach_network_policies([policy], comps)
    assert summaries[0]["components"] == ["deployment/web"]


def test_projected_volumes_count_as_config_references():
    obj = {
        "kind": "Deployment",
        "metadata": {"name": "x", "namespace": "ns"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "x"}},
            "template": {
                "metadata": {"labels": {"app": "x"}},
                "spec": {
                    "containers": [{"name": "x", "image": "i"}],
                    "volumes": [{"name": "cfg", "projected": {"sources": [{"configMap": {"name": "x-config"}}]}}],
                },
            },
        },
        "status": {},
    }
    assert collector.summarize_workload(obj)["config_refs"]["configmaps"] == ["x-config"]


def test_quoted_text_in_a_pointer_signal_is_not_a_change_and_alerts_are_symptoms():
    comps = {
        "deployment/web": {
            "signals": [
                "errors logged by 1 other component(s) (api) name this component (connectivity), e.g. 'session created '",
                "firing alert KubePodNotReady",
                "service web selects this component but has 0 ready endpoints",
            ]
        }
    }
    derive.classify_evidence(comps)
    assert comps["deployment/web"]["evidence_kinds"] == ["pointed_at", "symptoms"]


def test_label_selector_semantics_cover_expressions():
    assert checks.label_selector_matches({"matchExpressions": [{"key": "a", "operator": "Exists"}]}, {"a": "1"})


def _meta_with_later_write(fields: dict, applied: dict | None = None) -> dict:
    import json

    meta = {
        "creationTimestamp": "2026-10-19T08:00:00Z",
        "managedFields": [
            {"manager": "helm", "operation": "Update", "time": "2026-10-19T08:00:00Z", "fieldsV1": {"f:data": {}}},
            {"manager": "kubectl-patch", "operation": "Update", "time": "2026-10-19T08:10:00Z", "fieldsV1": fields},
        ],
    }
    if applied is not None:
        meta["annotations"] = {"kubectl.kubernetes.io/last-applied-configuration": json.dumps(applied)}
    return meta


def test_a_change_states_the_value_it_set_and_the_old_one_when_kubernetes_kept_it():
    obj = {"kind": "BGPPeer", "spec": {"peerSelector": "has(node-role.kubernetes.io/master)", "asNumber": 64512}}
    meta = _meta_with_later_write({"f:spec": {"f:peerSelector": {}}})
    assert changes.changed_values(obj, meta) == ["spec.peerSelector = has(node-role.kubernetes.io/master)"]
    applied = {"spec": {"peerSelector": "has(node-role.kubernetes.io/control-plane)"}}
    meta = _meta_with_later_write({"f:spec": {"f:peerSelector": {}}}, applied)
    assert changes.changed_values(obj, meta) == [
        "spec.peerSelector: has(node-role.kubernetes.io/control-plane) -> has(node-role.kubernetes.io/master)"
    ]


def test_a_long_text_change_is_shown_where_it_differs_and_secrets_are_never_read():
    corefile = ".:53 {\n" + "    errors\n" * 40 + "    forward . /etc/resolv.conf\n}"
    obj = {
        "kind": "ConfigMap",
        "data": {"Corefile": corefile.replace("forward . /etc/resolv.conf", "forward . 8.8.8.8")},
    }
    meta = _meta_with_later_write({"f:data": {"f:Corefile": {}}}, {"data": {"Corefile": corefile}})
    (value,) = changes.changed_values(obj, meta)
    assert value.startswith("data.Corefile: …") and "8.8.8.8" in value and "/etc/resolv.conf" in value
    secret = {"kind": "Secret", "data": {"password": "c2VjcmV0"}}
    assert changes.changed_values(secret, _meta_with_later_write({"f:data": {"f:password": {}}})) == []
    assert changes.object_content(secret) == "keys password"


def test_list_items_are_found_by_name_in_a_changed_path():
    obj = {"spec": {"containers": [{"name": "app", "env": [{"name": "MODE", "value": "fast"}]}]}}
    assert changes.value_at(obj, "spec.containers[app].env[MODE].value") == "fast"
    assert changes.value_at(obj, "spec.containers[other].image") is changes._MISSING


def test_an_object_created_after_the_application_states_what_it_holds():
    pc = {"kind": "PriorityClass", "value": 1000000, "globalDefault": True, "metadata": {"name": "batch-critical"}}
    from clients.jev_diag.timeutil import set_now

    meta = {"creationTimestamp": "2026-10-19T08:10:00Z", "managedFields": []}
    set_now(parse_time("2026-10-19T08:11:00Z"))  # changes older than an hour are not listed
    try:
        change = changes.object_change(
            "priorityclass/batch-critical",
            "PriorityClass",
            "batch-critical",
            None,
            meta,
            targets_created=["2026-10-19T08:00:00Z"],
            obj=pc,
        )
    finally:
        set_now(None)
    assert change and '"globalDefault": true' in change["content"] and '"value": 1000000' in change["content"]
    assert "it holds" in changes.describe_change({**change, "seconds_ago": 30})


def test_a_flag_turned_on_is_a_change_on_the_service_it_names_not_on_the_flag_server():
    import json

    from clients.jev_diag.v2 import flags

    doc = {
        "flags": {
            "paymentServiceFailure": {
                "state": "ENABLED",
                "variants": {"on": True, "off": False},
                "defaultVariant": "on",
            },
            "cartCacheFailure": {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "off"},
            "adHighCpu": {"state": "DISABLED", "variants": {"on": True}, "defaultVariant": "on"},
        }
    }
    comps = {
        f"deployment/{n}": {"name": n, "namespace": "shop", "signals": []}
        for n in ("payment", "cart", "ad", "flag-server", "payment-gateway")
    }
    config = {("ConfigMap", "shop", "flags"): {"_object": {"data": {"flags.json": json.dumps(doc)}}}}
    flags.attribute_flags(comps, config, {"configmap/shop/flags"})
    assert comps["deployment/payment"]["signals"][0].startswith("feature flag paymentServiceFailure is on")
    assert not any(comps[c]["signals"] for c in comps if c != "deployment/payment")
    derive.classify_evidence(comps)
    assert comps["deployment/payment"]["evidence_kinds"] == ["change"]
    # a flag ConfigMap not written after deploy holds the baseline: nothing is attributed
    comps["deployment/payment"]["signals"] = []
    flags.attribute_flags(comps, config, set())
    assert not comps["deployment/payment"]["signals"]
    assert flags.key_tokens("loadGeneratorFloodHomepage") == {"load", "generator", "flood", "homepage"}
