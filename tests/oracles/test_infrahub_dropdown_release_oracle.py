"""Verdicts of the Infrahub dropdown-release mitigation oracle against a scripted application."""

import json
from types import SimpleNamespace

from sregym.conductor.oracles.apps.infrahub.dropdown_release import InfrahubDropdownReleaseOracle

TOPOLOGY = {"infrahub-server": 1, "task-worker": 2}
ORIGINAL = {"site": ["dc1", "dc2", "dc3"], "status": ["active", "planned"]}
RELEASED = {"site": ["dc4"], "status": ["rma"]}
FULL = {"site": ["dc1", "dc2", "dc3", "dc4"], "status": ["active", "planned", "rma"]}
LOST = {"site": ["dc1", "dc2", "dc3"], "status": ["active", "planned", "rma"]}
CONFIGURED = {"NetworkDevice": ["role", "site", "status"], "values": FULL}
BASELINE = {"volumes": {"database-data": "uid-1"}, "devices": ["core-000"], "modules": ["core-000-m1"]}


def view(choices):
    return json.dumps({a: sorted(c) for a, c in choices.items()}, sort_keys=True)


class FakeApp:
    topology = TOPOLOGY

    def __init__(self, views=None, configured=None, probe_ok=True, missing=(), replicas=None, uids=("a", "b")):
        self.views = views or {view(FULL): 32}
        self.configured = CONFIGURED if configured is None else configured
        self.probe_ok = probe_ok
        self.missing = list(missing)
        self.replicas = {**TOPOLOGY, **(replicas or {})}
        self.uids = uids

    def command(self, *args, **kwargs):
        replicas = self.replicas[args[2]]
        status = {"observedGeneration": 1, "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas}
        return json.dumps({"metadata": {"generation": 1}, "spec": {"replicas": replicas}, "status": status})

    def configured_inventory_fields(self):
        return self.configured

    def redis(self, *args, **kwargs):
        return ""

    def volume_ids(self):
        return {"database-data": "uid-1"}

    def pods(self, app):
        return [
            {"metadata": {"uid": u, "name": f"{app}-{u}"}, "status": {"containerStatuses": [{"restartCount": 0}]}}
            for u in self.uids
        ]

    def run_client(self, mode, **kwargs):
        if mode == "missing_devices":
            return {"missing": self.missing}
        if mode == "choice_views":
            return self.views
        if mode == "probe_choices":
            return {"ok": self.probe_ok, "results": [{"ok": self.probe_ok, "field": "site", "value": "dc4"}]}
        raise AssertionError(mode)


def dropdown_oracle(app, released=True):
    oracle = InfrahubDropdownReleaseOracle(SimpleNamespace(app=app, release_started=released), ORIGINAL, RELEASED)
    oracle.readiness_grace_seconds = 0
    oracle.baseline = BASELINE
    oracle.capture_handover()
    return oracle


def test_dropdown_oracle_passes_with_every_choice_on_every_worker():
    verdict = dropdown_oracle(FakeApp()).evaluate()
    assert verdict["success"], verdict
    assert verdict["collateral"] == {"restarted": []}


def test_dropdown_oracle_fails_on_the_lost_choice_and_still_reports_every_check():
    verdict = dropdown_oracle(FakeApp(views={view(LOST): 32}, probe_ok=False)).evaluate()
    assert verdict["reason"] == "released_choice_missing"
    assert verdict["checks"]["schema"]["released_choices_missing"] == ["site=dc4"]
    assert verdict["checks"]["probe_devices"]["ok"] is False
    assert verdict["checks"]["pre_incident_objects"]["ok"] is True


def test_dropdown_oracle_requires_nothing_released_before_the_release():
    original = {"site": ["dc1", "dc2", "dc3"], "status": ["active", "planned"]}
    assert dropdown_oracle(FakeApp(views={view(original): 32}), released=False).evaluate()["success"]


def test_dropdown_oracle_rejects_turning_the_release_off():
    configured = {"NetworkDevice": ["role", "site", "status"], "values": LOST}
    verdict = dropdown_oracle(FakeApp(configured=configured)).evaluate()
    assert verdict["reason"] == "release_turned_off"


def test_dropdown_oracle_rejects_dropping_an_existing_choice_or_the_dropdown():
    dropped = {"site": ["dc2", "dc3", "dc4"], "status": ["active", "planned", "rma"]}
    assert dropdown_oracle(FakeApp(views={view(dropped): 32})).evaluate()["reason"] == "existing_choice_removed"
    text = {"status": ["active", "planned", "rma"]}
    assert dropdown_oracle(FakeApp(views={view(text): 32})).evaluate()["reason"] == "dropdown_attribute_changed"


def test_dropdown_oracle_rejects_workers_that_disagree():
    verdict = dropdown_oracle(FakeApp(views={view(FULL): 24, view({**FULL, "status": ["active"]}): 8})).evaluate()
    assert verdict["success"] is False


def test_dropdown_oracle_reports_a_restart_as_collateral_only():
    app = FakeApp()
    oracle = dropdown_oracle(app)
    app.uids = ("c", "d")
    verdict = oracle.evaluate()
    assert verdict["success"]
    assert verdict["collateral"]["restarted"] == ["infrahub-server", "task-worker"]


def test_dropdown_oracle_rejects_lost_inventory():
    assert dropdown_oracle(FakeApp(missing=["core-000"])).evaluate()["reason"] == "pre_incident_objects_missing"
