"""Verdicts of the Infrahub reopened-release mitigation oracle against a scripted application."""

import json
from types import SimpleNamespace

from sregym.conductor.oracles.apps.infrahub.reopened_release import InfrahubReopenedReleaseOracle

TOPOLOGY = {"infrahub-server": 1, "task-worker": 2}
FULL = {"site": ["dc1", "dc2", "dc3", "dc4"], "status": ["active", "planned", "rma"]}
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


class MergeApp(FakeApp):
    def __init__(self, branches=None, partial=0, writable=True, missing=(), log_lines=(), expansion="OPEN", **kw):
        super().__init__(missing=missing, **kw)
        self.branches = {"main": "OPEN", "change-1": "MERGED", **(branches or {})}
        self.partial = partial
        self.writable = writable
        self.log_lines = list(log_lines)
        self.expansion = expansion
        self.lost = set()

    def cypher(self, query):
        return [str(self.partial)]

    def logs(self, pod, since=None):
        return "\n".join(json.dumps(e) for e in self.log_lines)

    def run_client(self, mode, **kwargs):
        if mode == "missing_devices":
            return {"missing": [n for n in kwargs["names"] if n in self.lost or n in self.missing]}
        if mode == "branches":
            return self.branches
        if mode == "inventory":
            return {"names": ["core-000", "inv-1", None], "modules": ["core-000-m1"], "count": 3}
        if mode == "probe_devices":
            return {"ok": self.writable, "results": [{"ok": self.writable, "error": None if self.writable else "x"}]}
        if mode == "branch_status":
            return {"status": self.expansion if kwargs["branch"] == "dc-expansion" else self.branch_state}
        if mode == "branch_change":
            return {"branch": "release-x", "merged": True}
        if mode == "kind_attributes":
            return {"attributes": self.attributes}
        return super().run_client(mode, **kwargs)


# ---------------------------------------------------------------- reopened release (CR-13)
def release_app(
    state="MERGED", attributes=("eol_date", "support_end"), plan=("security-zones", "lifecycle-eol"), log_lines=()
):
    app = MergeApp(log_lines=log_lines)
    app.branch_state = state
    app.attributes = list(attributes)
    app.configured = {"NetworkDevice": ["role", "site"], "releases": list(plan)}
    return app


def release_oracle(app, released=True):
    oracle = InfrahubReopenedReleaseOracle(
        SimpleNamespace(app=app, release_started=released), "lifecycle-eol", ["eol_date", "support_end"], ["eol-00"]
    )
    oracle.readiness_grace_seconds = 0
    oracle.observe_seconds = 0
    oracle.baseline = BASELINE
    oracle.capture_handover()
    return oracle


FAILED_RELEASE = [{"op": "release", "ok": False, "branch": "lifecycle-eol", "error": "merge task FAILED"}]


def test_release_oracle_passes_once_the_branch_is_merged_or_deleted():
    assert release_oracle(release_app()).evaluate()["success"]
    assert release_oracle(release_app(state=None)).evaluate()["success"]


def test_release_oracle_fails_while_the_branch_is_open_and_re_merges_fail():
    verdict = release_oracle(release_app(state="OPEN", log_lines=FAILED_RELEASE)).evaluate()
    assert verdict["reason"] == "release_branch_still_open"
    assert verdict["checks"]["release_merges_quiet"]["ok"] is False


def test_release_oracle_rejects_dropping_the_release():
    assert release_oracle(release_app(plan=("security-zones",))).evaluate()["reason"] == "release_turned_off"
    verdict = release_oracle(release_app(attributes=("eol_date",))).evaluate()
    assert verdict["reason"] == "release_missing_on_main"


def test_release_oracle_needs_nothing_before_the_release():
    assert release_oracle(release_app(state="OPEN", plan=("security-zones",)), released=False).evaluate()["success"]
