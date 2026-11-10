"""Verdicts of the Infrahub schema-release mitigation oracle against a scripted application."""

import json
from types import SimpleNamespace

from sregym.conductor.oracles.apps.infrahub.schema_release import InfrahubSchemaReleaseOracle

TOPOLOGY = {"infrahub-server": 1, "task-worker": 2}
BASE = {"NetworkDevice": ["role", "site"], "NetworkModule": ["device", "slot"]}
RELEASED = {"NetworkDevice": ["serial_number", "firmware_version"], "NetworkModule": ["serial_number"]}
FULL_VIEW = "firmware_version,name,role,serial_number,site"
FULL_MODULE = "device,name,serial_number,slot"
CONFIGURED = {kind: BASE[kind] + RELEASED[kind] for kind in BASE}


class FakeApp:
    topology = TOPOLOGY

    def __init__(
        self,
        replicas=None,
        views=None,
        modules=None,
        probe_ok=True,
        missing=(),
        worker_uids=("a", "b"),
        configured=None,
    ):
        self.replicas = {**TOPOLOGY, **(replicas or {})}
        self.views = {"NetworkDevice": views or {FULL_VIEW: 32}, "NetworkModule": modules or {FULL_MODULE: 32}}
        self.configured = CONFIGURED if configured is None else configured
        self.probe_ok = probe_ok
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

    def configured_inventory_fields(self):
        return self.configured

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
        if mode == "views":
            return {kind: self.views[kind] for kind in kwargs["kinds"]}
        if mode in ("probe_devices", "probe_objects"):
            return {"ok": self.probe_ok, "results": [{"ok": self.probe_ok}]}
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


def schema_oracle(app, released=True):
    problem = SimpleNamespace(app=app, releases_started=released)
    oracle = InfrahubSchemaReleaseOracle(problem, BASE, RELEASED)
    oracle.readiness_grace_seconds = 0
    oracle.baseline = BASELINE
    return oracle


def test_schema_oracle_passes_when_every_worker_serves_both_attributes():
    assert schema_oracle(FakeApp()).evaluate()["success"]


def test_schema_oracle_requires_nothing_before_the_release_window():
    app = FakeApp(views={"name,role,site": 32}, modules={"device,name,slot": 32}, configured=BASE)
    assert schema_oracle(app, released=False).evaluate()["success"]


def test_schema_oracle_fails_on_the_lost_attribute():
    verdict = schema_oracle(FakeApp(views={"firmware_version,name,role,site": 32})).evaluate()
    assert verdict["reason"] == "released_attribute_missing"
    assert verdict["detail"]["missing"] == ["serial_number"]


def test_schema_oracle_fails_when_one_worker_is_stale():
    verdict = schema_oracle(FakeApp(views={FULL_VIEW: 24, "firmware_version,name,role,site": 8})).evaluate()
    assert verdict["reason"] == "api_workers_disagree_on_schema"


def test_schema_oracle_fails_when_one_worker_serves_a_stale_module_schema():
    verdict = schema_oracle(FakeApp(modules={FULL_MODULE: 24, "device,name,slot": 8})).evaluate()
    assert verdict["reason"] == "api_workers_disagree_on_schema"
    assert verdict["detail"]["kind"] == "NetworkModule"


def test_schema_oracle_rejects_turning_the_release_off():
    configured = {"NetworkDevice": ["role", "site", "firmware_version"], "NetworkModule": ["device", "slot"]}
    verdict = schema_oracle(FakeApp(views={"firmware_version,name,role,site": 32}, configured=configured)).evaluate()
    assert verdict["reason"] == "release_turned_off"


def test_schema_oracle_rejects_lost_inventory_and_scaled_down_components():
    assert schema_oracle(FakeApp(missing=["core-000"])).evaluate()["reason"] == "pre_incident_objects_missing"
    verdict = schema_oracle(FakeApp(replicas={"task-worker": 0})).evaluate()
    assert verdict["reason"] == "required_application_topology_changed"
