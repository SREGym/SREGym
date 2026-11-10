"""The merged release must be recognized as merged again, with its changes intact on main.

Checks (every one runs and is reported in ``checks``; the reason is the first
failure in this order):

- platform invariants: topology, volumes, every pre-incident device and module;
- ``release_plan_kept``: inventory-sync's release plan still lists the release
  (taking it off the plan only silences the symptom);
- ``release_on_main``: main's NetworkDevice schema has every attribute the
  release added and the release's devices are on main (undoing the release, for
  example by removing its attributes, fails);
- ``branch_not_open``: the release branch is no longer OPEN (MERGED again, or
  deleted now that its changes are on main) and stays so while inventory-sync's
  release check runs;
- ``release_merges_quiet``: inventory-sync logs no failed release merge during
  that observation window;
- ``main_writable`` and ``merges_work``: a device can be created on main and a
  fresh change branch merges.

Restarting components is harmless and only reported as ``collateral``.
"""

import json
import time
from datetime import UTC, datetime

from sregym.conductor.oracles.apps.infrahub.platform import InfrahubPlatformOracle
from sregym.conductor.oracles.failure import FailureClass


class InfrahubReopenedReleaseOracle(InfrahubPlatformOracle):
    FAILURE_CLASSES = {
        "release_turned_off": FailureClass.AGENT_ERROR,
        "release_missing_on_main": FailureClass.AGENT_ERROR,
        "release_branch_still_open": FailureClass.AGENT_ERROR,
        "release_merges_still_failing": FailureClass.AGENT_ERROR,
        "main_not_writable": FailureClass.AGENT_ERROR,
        "merge_did_not_complete": FailureClass.AGENT_ERROR,
    }
    evaluation_timeout_seconds = 1200
    #: Longer than inventory-sync's release check period (60 s) plus one merge attempt.
    observe_seconds = 150

    def __init__(self, problem, branch, attributes, devices):
        super().__init__(problem)
        self.branch = branch
        self.attributes = list(attributes)
        self.devices = list(devices)

    @property
    def released(self):
        return getattr(self.problem, "release_started", False)

    def plan_check(self):
        plan = (self.app.configured_inventory_fields() or {}).get("releases", [])
        if not self.released:
            return {"ok": True, "released": False, "plan": plan}
        return {"ok": self.branch in plan, "plan": plan}

    def on_main_check(self):
        if not self.released:
            return {"ok": True, "released": False}
        attributes = self.app.run_client("kind_attributes")["attributes"]
        missing_attributes = sorted(set(self.attributes) - set(attributes))
        missing_devices = self.app.run_client("missing_devices", names=self.devices)["missing"]
        return {
            "ok": not missing_attributes and not missing_devices,
            "attributes_missing": missing_attributes,
            "devices_missing": missing_devices,
        }

    def observe(self, checks):
        """Watch the branch and inventory-sync's release checks for a while."""
        started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        statuses = []
        deadline = time.monotonic() + self.observe_seconds
        while True:
            statuses.append(self.app.run_client("branch_status", branch=self.branch)["status"])
            if time.monotonic() >= deadline:
                break
            time.sleep(15)
        failures = []
        for pod in self.app.pods("inventory-sync"):
            try:
                logs = self.app.logs(pod["metadata"]["name"], since=started)
            except Exception:
                continue
            for line in logs.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("op") == "release" and not event.get("ok"):
                    failures.append(event)
        reopened = [s for s in statuses if s == "OPEN"]
        if not self.released:
            checks["branch_not_open"] = {"ok": True, "released": False}
        else:
            checks["branch_not_open"] = {"ok": not reopened, "statuses": sorted(set(map(str, statuses)))}
        checks["release_merges_quiet"] = {"ok": not failures, "failed_release_merges": failures[:3]}

    def writable_check(self):
        probe = self.app.run_client("probe_devices", fields=["role"], count=2, exec_timeout=300)
        return {"ok": probe["ok"], "errors": [r.get("error") for r in probe["results"] if not r["ok"]][:2]}

    def merges_check(self):
        result = self.app.run_client("branch_change", devices=1, timeout=300)
        return {"ok": bool(result["merged"]), "result": result}

    def evaluate(self):
        if not self.baseline:
            return self.fail("baseline_not_captured")
        checks = {}
        self.platform_checks(checks)
        self.run_check(checks, "release_plan_kept", self.plan_check)
        self.run_check(checks, "release_on_main", self.on_main_check)
        try:
            self.observe(checks)
        except Exception as exc:
            checks.setdefault("branch_not_open", {"ok": None, "error": str(exc)[-300:]})
            checks.setdefault("release_merges_quiet", {"ok": None, "error": str(exc)[-300:]})
        self.run_check(checks, "main_writable", self.writable_check)
        self.run_check(checks, "merges_work", self.merges_check)
        order = [
            *self.PLATFORM_ORDER,
            ("release_plan_kept", "release_turned_off"),
            ("release_on_main", "release_missing_on_main"),
            ("branch_not_open", "release_branch_still_open"),
            ("release_merges_quiet", "release_merges_still_failing"),
            ("main_writable", "main_not_writable"),
            ("merges_work", "merge_did_not_complete"),
        ]
        return self.verdict(checks, order, self.collateral())
