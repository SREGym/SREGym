"""Azure Central US (July 2024) on OpenStack: an incomplete host allow-list pushed region-wide.

An inventory migration republishes one rack's host inventory under a new name.
The host-access reconciler mounts rack inventories as optional sources, so it
builds its allow list without that rack and deletes those hypervisors' broker
users. Their nova-compute services lose the message bus and Nova marks them
down, while their pods stay healthy. The reconciler runs every minute, so
re-creating the users by hand is undone unless its input is fixed or it is
halted. See scenarios/host_access_revocation.yaml for the mapping and deviations.
"""

from __future__ import annotations

import json
import time

from sregym.conductor.oracles.apps.openstack.region_recovery import INVARIANTS, OpenStackRegionRecoveryOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.apps.openstack import scenario
from sregym.conductor.problems.base import Problem
from sregym.service.apps.openstack.application import OpenStackCloud, rack_inventory_name
from sregym.utils.decorators import mark_fault_injected


def revoked_hosts(reconciler_log: str) -> set[str]:
    prefix = "revoked broker access: "
    return {line[len(prefix) :].split()[0] for line in reconciler_log.splitlines() if line.startswith(prefix)}


class OpenStackHostAccessRevocationProblem(Problem):
    scenario_name = "host_access_revocation"

    def __init__(self, scale_tier: str = "small"):
        super().__init__(OpenStackCloud(tier=scale_tier))
        self.scenario = scenario.load(self.scenario_name, known_invariants=set(INVARIANTS))
        self.params = self.scenario.fault(scale_tier)
        tier = self.app.tier
        self.rack = self.params["rack"]
        if self.rack not in tier.racks:
            raise ValueError(f"Rack {self.rack!r} is not in tier {tier.name}")
        self.hosts = [h for h in tier.hypervisors if tier.rack_of(h) == self.rack]
        self.source = rack_inventory_name(self.rack)
        self.successor = self.source + self.params["successor_suffix"]
        self.faulty_service = "host-access-reconciler"
        # Long enough for a full tenant SLO window before the fault.
        self.baseline_duration_s = int(self.scenario.grading["window_s"]) + 120
        self.propagation_duration_s = 30
        diagnosis = self.scenario.diagnosis(
            rack=self.rack, hosts=", ".join(self.hosts), source=self.source, successor=self.successor
        )
        self.root_cause = self.build_structured_root_cause(
            component=diagnosis["component"], namespace=self.namespace, description=diagnosis["description"]
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = OpenStackRegionRecoveryOracle(self, self.scenario)
        self.saved_inventory: dict | None = None
        #: What the injector did, in order, for the run record.
        self.actions: list[dict] = []

    def _act(self, action: str, **detail):
        self.actions.append({"time": time.time(), "action": action, **detail})

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        original = json.loads(app.command("get", "configmap", self.source, "-o", "json"))
        self.saved_inventory = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": self.source, "labels": original["metadata"].get("labels", {})},
            "data": original["data"],
        }
        inventory = json.loads(original["data"]["inventory.json"])
        inventory["exported_by"] = self.params["exported_by"]
        app.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": self.successor, "labels": original["metadata"].get("labels", {})},
                    "data": {"inventory.json": json.dumps(inventory, indent=2)},
                }
            ]
        )
        app.command("delete", "configmap", self.source)
        self._act("inventory_republished", source=self.source, successor=self.successor)
        # The CronJob would pick this up within a minute; run it now so the
        # incident starts at a known time.
        log = app.run_reconciler()
        revoked = revoked_hosts(log)
        self._act("reconciler_ran", revoked=sorted(revoked))
        if revoked != set(self.hosts):
            raise RuntimeError(f"Expected the reconciler to revoke {self.hosts}, it revoked {sorted(revoked)}:\n{log}")
        app.wait_hypervisors(self.hosts, state="down", timeout=240)
        self._act("hypervisors_down", hosts=self.hosts)

    @mark_fault_injected
    def recover_fault(self):
        """Return the region to health: restore the inventory, re-grant, unstick pets."""
        app = self.app
        if self.saved_inventory is None:
            return
        if not app.command("get", "configmap", self.source, "--ignore-not-found", "-o", "name").strip():
            app.apply([self.saved_inventory])
        app.command("patch", "cronjob", "host-access-reconciler", "-p", '{"spec":{"suspend":false}}')
        app.run_reconciler()
        app.wait_hypervisors(self.hosts, state="up", timeout=300)
        for pet in app.pets():
            if pet["task_state"] or pet["status"] not in ("ACTIVE",):
                app.toolbox("openstack", "server", "set", "--state", "active", pet["id"])
