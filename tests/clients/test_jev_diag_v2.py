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
