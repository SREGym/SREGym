"""Verdicts of the Infrahub branch-merge mitigation oracle against a scripted application."""

import json
from types import SimpleNamespace

from sregym.conductor.oracles.apps.infrahub.branch_merge import InfrahubBranchMergeOracle

TOPOLOGY = {"infrahub-server": 1, "task-worker": 2}


class FakeApp:
    topology = TOPOLOGY

    def __init__(self, replicas=None, missing=(), worker_uids=("a", "b")):
        self.replicas = {**TOPOLOGY, **(replicas or {})}
        self.missing = list(missing)
        self.worker_uids = worker_uids
        self.calls = []

    def command(self, *args, **kwargs):
        name = args[2]
        replicas = self.replicas[name]
        return json.dumps(
            {
                "metadata": {"generation": 1},
                "spec": {"replicas": replicas},
                "status": {
                    "observedGeneration": 1,
                    "replicas": replicas,
                    "updatedReplicas": replicas,
                    "readyReplicas": replicas,
                },
            }
        )

    def volume_ids(self):
        return {"database-data": "uid-1"}

    def pods(self, app):
        return [
            {"metadata": {"uid": uid}, "status": {"containerStatuses": [{"restartCount": 0}]}}
            for uid in self.worker_uids
        ]

    def redis(self, *args):
        return ""

    def run_client(self, mode, **kwargs):
        self.calls.append(mode)
        if mode == "missing_devices":
            return {"missing": self.missing}
        if mode == "branch_change":
            return {"branch": f"release-{len(self.calls)}", "merged": True}
        if mode == "touch_schema":
            return {"status": 200}
        if mode == "branches":
            return {"main": "OPEN"}
        raise AssertionError(mode)


BASELINE = {
    "volumes": {"database-data": "uid-1"},
    "devices": ["core-000"],
    "modules": ["core-000-m1"],
    "device_count": 1,
}


def merge_oracle(app, wedged_uid=None, healthy_uid=None):
    wedged = {"pod": "task-worker-x", "uid": wedged_uid, "restarts": 0} if wedged_uid else None
    healthy = {"pod": "task-worker-h", "uid": healthy_uid, "restarts": 0} if healthy_uid else None
    oracle = InfrahubBranchMergeOracle(SimpleNamespace(app=app, wedged_worker=wedged, healthy_worker=healthy))
    oracle.readiness_grace_seconds = 0
    oracle.baseline = BASELINE
    return oracle


def test_merge_oracle_fails_while_the_wedged_worker_runs():
    verdict = merge_oracle(FakeApp(worker_uids=("x", "b")), wedged_uid="x").evaluate()
    assert verdict["reason"] == "wedged_worker_still_running"


def test_merge_oracle_rejects_restarting_the_healthy_worker_too():
    verdict = merge_oracle(FakeApp(worker_uids=("c", "d")), wedged_uid="x", healthy_uid="b").evaluate()
    assert verdict["reason"] == "healthy_worker_restarted"


def test_merge_oracle_passes_once_merges_complete_on_replaced_workers():
    app = FakeApp(worker_uids=("c", "b"))
    verdict = merge_oracle(app, wedged_uid="x", healthy_uid="b").evaluate()
    assert verdict["success"], verdict
    assert app.calls.count("branch_change") == InfrahubBranchMergeOracle.probe_merges


def test_merge_oracle_rejects_lost_inventory_and_scaled_down_components():
    app = FakeApp(worker_uids=("c", "b"), missing=["core-000"])
    verdict = merge_oracle(app, wedged_uid="x", healthy_uid="b").evaluate()
    assert verdict["reason"] == "pre_incident_objects_missing"
    app = FakeApp(worker_uids=("c", "b"), replicas={"task-worker": 0})
    verdict = merge_oracle(app, wedged_uid="x", healthy_uid="b").evaluate()
    assert verdict["reason"] == "required_application_topology_changed"
