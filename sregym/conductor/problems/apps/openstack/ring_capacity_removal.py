"""AWS S3 (February 2017) on OpenStack Swift: a capacity-removal command with a bad input.

The drive-decommission runbook removes one device from the object ring. Its
host part expands empty, so the search value matches that device on every
storage node, and the rebalance moves every partition off those drives at once.
Objects whose replicas were all on them return 404 while their bytes are intact
on the removed drives. See scenarios/ring_capacity_removal.yaml for the mapping
and deviations.
"""

from __future__ import annotations

import time

from sregym.conductor.oracles.apps.openstack.region_recovery import INVARIANTS, OpenStackRegionRecoveryOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.apps.openstack import scenario
from sregym.conductor.problems.base import Problem
from sregym.service.apps.openstack import object_storage
from sregym.service.apps.openstack.application import OpenStackCloud
from sregym.utils.decorators import mark_fault_injected

BUILDER = f"{object_storage.BUILDER_DIR}/object.builder"
RING = f"{object_storage.BUILDER_DIR}/object.ring.gz"


class OpenStackRingCapacityRemovalProblem(Problem):
    scenario_name = "ring_capacity_removal"

    def __init__(self, scale_tier: str = "small"):
        super().__init__(OpenStackCloud(tier=scale_tier, object_storage=True))
        self.scenario = scenario.load(self.scenario_name, known_invariants=set(INVARIANTS))
        self.params = self.scenario.fault(scale_tier)
        tier = self.app.tier
        self.device = self.params["device"]
        if self.device not in object_storage.devices(tier):
            raise ValueError(f"Device {self.device!r} is not on tier {tier.name}'s storage nodes")
        node = tier.storage_nodes[self.params["intended_node"]]
        zone = self.params["intended_node"] + 1
        self.intended = f"z{zone}-{object_storage.node_host(self.app, node)}:6200/{self.device}"
        # The runbook interpolates the host; it expanded empty.
        self.typed = f"/{self.device}"
        self.removed = [f"{object_storage.node_host(self.app, n)}/{self.device}" for n in tier.storage_nodes]
        self.faulty_service = "swift-ring-builder"
        self.baseline_duration_s = int(self.scenario.grading["window_s"]) + 120
        self.propagation_duration_s = 30
        diagnosis = self.scenario.diagnosis(
            intended=self.intended, typed=self.typed, device=self.device, count=len(self.removed)
        )
        self.root_cause = self.build_structured_root_cause(
            component=diagnosis["component"], namespace=self.namespace, description=diagnosis["description"]
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = OpenStackRegionRecoveryOracle(self, self.scenario)
        self.injected_at: float | None = None
        self.actions: list[dict] = []

    def builder(self, *args) -> str:
        return self.app.command("exec", "deploy/swift-ring-builder", "--", *args, timeout=180)

    def ring_md5(self, target: str, container: str) -> str:
        output = self.app.command("exec", target, "-c", container, "--", "md5sum", "/etc/swift/object.ring.gz")
        return output.split()[0]

    def wait_ring_installed(self, timeout: int = 120):
        """Wait until the proxy and every storage node run the ring the builder serves."""
        wanted = self.builder("md5sum", RING).split()[0]
        targets = ["deploy/swift-proxy"] + [f"pod/{node}" for node in self.app.tier.storage_nodes]
        deadline = time.monotonic() + timeout
        while True:
            stale = [t for t in targets if self.ring_md5(t, "ring-sync") != wanted]
            if not stale:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"Ring not installed on {stale}")
            time.sleep(3)

    def unavailable_objects(self) -> list[str]:
        audit = self.app.object_audit({})
        return [f"{t}/{m}" for t, r in audit["tenants"].items() for m in r["missing"]]

    @mark_fault_injected
    def inject_fault(self):
        self.injected_at = time.time()
        # The change log records what an operator ran, as a change ticket would.
        note = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} DCOPS-2231 decommission failed drive "
            f"{self.intended}: swift-ring-builder object.builder remove $TARGET/{self.device} --yes && rebalance"
        )
        self.builder("sh", "-c", f"echo {note!r} >> {object_storage.BUILDER_DIR}/changes.log")
        removed = self.builder("swift-ring-builder", BUILDER, "remove", self.typed, "--yes")
        self.actions.append({"time": time.time(), "action": "remove", "search": self.typed, "output": removed[-2000:]})
        try:
            rebalanced = self.builder("swift-ring-builder", BUILDER, "rebalance")
        except Exception as exc:
            # Exit status 1 is "rebalanced with warnings".
            if getattr(exc, "returncode", None) != 1:
                raise
            rebalanced = exc.stdout
        self.actions.append({"time": time.time(), "action": "rebalance", "output": rebalanced[-2000:]})
        self.wait_ring_installed()
        deadline = time.monotonic() + 120
        while not (unavailable := self.unavailable_objects()):
            if time.monotonic() > deadline:
                raise RuntimeError("Removing the devices made no stored object unavailable")
            time.sleep(10)
        self.actions.append({"time": time.time(), "action": "objects_unavailable", "count": len(unavailable)})

    def pre_incident_backup(self) -> tuple[str, str]:
        """The newest builder and ring backups written before the injection."""
        names = self.builder("ls", f"{object_storage.BUILDER_DIR}/backups").split()

        def newest(suffix):
            stamps = [int(n.split(".")[0]) for n in names if n.endswith(suffix) and n.split(".")[0].isdigit()]
            before = [s for s in stamps if s < (self.injected_at or time.time())]
            if not before:
                raise RuntimeError(f"No pre-incident backup ending in {suffix}")
            return f"{object_storage.BUILDER_DIR}/backups/{max(before)}.{suffix}"

        return newest("object.builder"), newest("object.ring.gz")

    @mark_fault_injected
    def recover_fault(self):
        if self.injected_at is None:
            return
        builder, ring = self.pre_incident_backup()
        self.builder("cp", builder, BUILDER)
        self.builder("cp", ring, RING)
        self.wait_ring_installed()
