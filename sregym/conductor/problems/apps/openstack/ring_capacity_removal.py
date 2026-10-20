"""AWS S3 (February 2017) on OpenStack Swift: a capacity-removal command with a bad input.

The drive-decommission runbook removes one drive by device id (``d<id>``). The
operator entered ``/<name>`` instead, a device-name search that matches that
device on every storage node, and the rebalance moves every partition off those
drives at once. Objects whose replicas were all on them return 404 while their
bytes are intact on the removed drives. See scenarios/ring_capacity_removal.yaml
for the mapping and deviations.
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

FINGERPRINT = """
import hashlib, json, sys
from swift.common.ring import RingData
for path in sys.argv[1:]:
    ring = RingData.load(path)
    digest = hashlib.sha256(json.dumps(sorted(
        (d["id"], d["ip"], d["device"], d["weight"]) for d in ring.devs if d)).encode())
    for row in ring._replica2part2dev_id:
        digest.update(row.tobytes())
    print(path, digest.hexdigest())
"""
BUILDER = f"{object_storage.BUILDER_DIR}/object.builder"
RING = f"{object_storage.BUILDER_DIR}/object.ring.gz"


class OpenStackRingCapacityRemovalProblem(Problem):
    scenario_name = "ring_capacity_removal"

    def __init__(self, scale_tier: str = "small"):
        spec = scenario.load(self.scenario_name, known_invariants=set(INVARIANTS))
        params = spec.fault(scale_tier)
        super().__init__(OpenStackCloud(tier=scale_tier, object_storage=True, object_replicas=params["replicas"]))
        self.scenario = spec
        self.params = params
        tier = self.app.tier
        self.device = self.params["device"]
        if self.device not in object_storage.devices(tier):
            raise ValueError(f"Device {self.device!r} is not on tier {tier.name}'s storage nodes")
        node_index = self.params["intended_node"]
        # Devices are added node by node (see swift_rings.py), so this is the
        # failed drive's id.
        device_id = node_index * tier.swift_devices_per_node + object_storage.devices(tier).index(self.device)
        self.intended = f"d{device_id}"
        self.intended_drive = f"{object_storage.node_host(self.app, tier.storage_nodes[node_index])}/{self.device}"
        # One character: a device-name search instead of a device id.
        self.typed = f"/{self.device}"
        self.removed = [f"{object_storage.node_host(self.app, n)}/{self.device}" for n in tier.storage_nodes]
        self.faulty_service = "swift-ring-builder"
        self.baseline_duration_s = int(self.scenario.grading["window_s"]) + 120
        self.propagation_duration_s = 30
        diagnosis = self.scenario.diagnosis(
            intended=self.intended,
            drive=self.intended_drive,
            typed=self.typed,
            device=self.device,
            count=len(self.removed),
        )
        self.root_cause = self.build_structured_root_cause(
            component=diagnosis["component"], namespace=self.namespace, description=diagnosis["description"]
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = OpenStackRegionRecoveryOracle(self, self.scenario)
        self.pre_ring_fingerprint: str | None = None
        self.saved: dict[str, str] = {}
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
        self.pre_ring_fingerprint = self.ring_fingerprints(RING)[RING]
        # Kept by the harness, outside the agent's reach, for recover_fault.
        self.saved = {path: self.builder("base64", "-w0", path) for path in (BUILDER, RING)}
        # The change log records what an operator ran, as a change ticket would.
        note = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} DCOPS-2231 decommission failed drive "
            f"{self.intended_drive} (id {self.intended[1:]}): "
            f"swift-ring-builder object.builder remove {self.typed} --yes && rebalance"
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

    def ring_fingerprints(self, *paths: str) -> dict[str, str]:
        """Content fingerprints (devices and partition table) of ring files.

        Backups are separate gzip files, so their bytes differ from the served
        ring even when the ring is the same.
        """
        output = self.builder("python", "-c", FINGERPRINT, *paths)
        return dict(line.split() for line in output.splitlines() if line.strip())

    def pre_incident_backup(self) -> tuple[str, str]:
        """The backed-up ring identical to the one served before the injection, and its builder.

        swift-ring-builder snapshots the new builder and ring into backups/ on
        every change, so after several changes "the newest backup" is not the
        good one; match the ring by content instead.
        """
        backups = f"{object_storage.BUILDER_DIR}/backups"
        names = self.builder("ls", backups).split()
        rings = self.ring_fingerprints(*(f"{backups}/{n}" for n in names if n.endswith("object.ring.gz")))
        matches = sorted(path for path, digest in rings.items() if digest == self.pre_ring_fingerprint)
        if not matches:
            raise RuntimeError("No backup matches the pre-incident object ring")
        stamp = matches[-1].rsplit("/", 1)[1].split(".")[0]
        builders = sorted(n for n in names if n.endswith("object.builder") and n.split(".")[0] <= stamp)
        return f"{backups}/{builders[-1]}", matches[-1]

    @mark_fault_injected
    def recover_fault(self):
        if not self.saved:
            return
        for path, data in self.saved.items():
            self.app.command(
                "exec", "-i", "deploy/swift-ring-builder", "--", "sh", "-c", f"base64 -d > {path}", input_text=data
            )
        self.wait_ring_installed()
