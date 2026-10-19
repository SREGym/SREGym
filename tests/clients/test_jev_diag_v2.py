"""jev_diag v2: shared plumbing with v1, and the driver hand-over."""

from __future__ import annotations

import argparse
from pathlib import Path

import msgspec

from clients.jev_diag import collector as v1_collector
from clients.jev_diag import driver as v1_driver
from clients.jev_diag.classifier import build_component_questions as v1_questions
from clients.jev_diag.v2 import collector as v2_collector
from clients.jev_diag.v2 import driver as v2_driver
from clients.jev_diag.v2.classifier import build_component_questions as v2_questions

NS = "shop"


def deployment(name: str, ready: int = 1) -> dict:
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NS, "uid": f"uid-{name}"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [{"name": name, "image": "i"}]}},
        },
        "status": {"replicas": 1, "readyReplicas": ready},
    }


def snapshot(module) -> object:
    comps = {}
    for obj in (deployment("frontend", ready=0), deployment("cart")):
        c = module.summarize_workload(obj)
        comps[module.component_id(c["kind"], c["name"])] = c
    comps["deployment/frontend"]["signals"] = ["only 0 of 1 desired replicas are ready"]
    for c in comps.values():
        c["healthy"] = not c.get("signals")
    return module.ClusterSnapshot(
        app={"name": "shop", "namespaces": [NS], "description": "d"},
        components=comps,
        cluster={"nodes": [], "firing_alerts": []},
    )


def test_v2_reads_the_cluster_through_v1s_single_kubectl_runner():
    # recording, replay and tracing are set up once by the driver and must cover v2's reads too
    assert v2_collector.run_kubectl is v1_collector.run_kubectl
    assert v2_collector.set_kubectl_replay is v1_collector.set_kubectl_replay


def test_v2_triage_question_matches_v1_on_the_same_snapshot():
    a = {k: msgspec.to_builtins(q) for k, q in v1_questions(snapshot(v1_collector)).items()}
    b = {k: msgspec.to_builtins(q) for k, q in v2_questions(snapshot(v2_collector)).items()}
    assert a == b


def test_driver_hands_mode_v2_to_the_v2_run(monkeypatch):
    seen = {}

    def fake_run(args, trace, prefix, results, started, host):
        seen["host"] = host
        return "ok"

    monkeypatch.setattr(v2_driver, "run", fake_run)
    args = argparse.Namespace(mode="v2")
    assert v1_driver.run(args, None, Path("/tmp/x"), {}, 0.0) == "ok"
    assert seen["host"].submit_diagnosis is v1_driver.submit_diagnosis


def _comp(name: str, created: str, **extra) -> dict:
    return {"kind": "Deployment", "name": name, "namespace": NS, "created": created, "signals": [], **extra}


def test_revision_diff_without_a_spec_write_is_a_change_and_a_restart_is_not():
    from clients.jev_diag.timeutil import set_now
    from clients.jev_diag.v2 import changes

    set_now(changes.parse_time("2026-10-19T08:00:00Z"))
    try:
        comps = {
            "deployment/frontend": _comp("frontend", "2026-10-19T07:50:00Z"),
            "daemonset/agent": {**_comp("agent", "2026-10-19T07:50:00Z"), "kind": "DaemonSet"},
        }
        removed = changes.object_change(
            "deployment/frontend",
            "Deployment",
            "frontend",
            NS,
            {"creationTimestamp": "2026-10-19T07:50:00Z"},
            affects=["deployment/frontend"],
            extra={"template_changes": ["container frontend env CART_ADDR removed"]},
            revised_at="2026-10-19T07:58:00Z",
        )
        restart = changes.object_change(
            "daemonset/agent",
            "DaemonSet",
            "agent",
            NS,
            {"creationTimestamp": "2026-10-19T07:50:00Z"},
            affects=["daemonset/agent"],
            extra={
                "template_changes": ["template annotation kubectl.kubernetes.io/restartedAt added: x"],
                "restart_only": True,
            },
            revised_at="2026-10-19T07:59:00Z",
        )
        cluster = {"_changes": [removed, restart]}
        changes.rank_changes(comps, cluster)
    finally:
        set_now(None)
    assert any(
        s.startswith("pod template changed") and "CART_ADDR removed" in s
        for s in comps["deployment/frontend"]["signals"]
    )
    assert comps["daemonset/agent"]["signals"][0].startswith("rolling restart")
    assert comps["deployment/frontend"].get("most_recent_change") and not comps["daemonset/agent"].get(
        "most_recent_change"
    )


def test_a_change_made_before_the_application_existed_is_dropped():
    from clients.jev_diag.timeutil import set_now
    from clients.jev_diag.v2 import changes

    set_now(changes.parse_time("2026-10-19T08:00:00Z"))
    try:
        comps = {"deployment/frontend": _comp("frontend", "2026-10-19T07:50:00Z")}
        leftover = {
            "object": "configmap/kube-system/coredns",
            "kind": "ConfigMap",
            "name": "coredns",
            "namespace": "kube-system",
            "at": "2026-10-19T07:30:00Z",
            "affects": ["deployment/frontend"],
            "modified_after_creation_s": 100,
        }
        cluster = {"_changes": [leftover]}
        ranked = changes.rank_changes(comps, cluster)
    finally:
        set_now(None)
    assert ranked == [] and cluster["recent_changes"] == [] and comps["deployment/frontend"]["signals"] == []


def test_probe_description_carries_timing_with_defaults_for_unset_fields():
    from clients.jev_diag.v2.checks import describe_probe

    text = describe_probe({"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 1, "failureThreshold": 1})
    assert text.startswith("httpGet HTTP :8080/health")
    assert "periodSeconds=1" in text and "failureThreshold=1" in text
    assert "initialDelaySeconds=0 (default)" in text and "timeoutSeconds=1 (default)" in text
