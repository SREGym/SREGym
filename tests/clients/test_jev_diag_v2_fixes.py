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
