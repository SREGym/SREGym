"""Grade an OpenStack region by what tenants and the fleet experience, not by commands run.

The oracle snapshots the healthy region before the fault: every hypervisor's
service state and admin status, every tenant pet server with its host, and a
tenant SLO window from the workload ledger. After the agent submits, it waits a
soak period, then observes a stability window, sampling the fleet throughout and
reading the tenant ledger for operations that started inside the window.

Invariants are named so a scenario can select the ones its incident needs:

- ``hypervisors_up``: every baseline hypervisor reports up in every sample.
- ``hypervisor_status_as_baseline``: no hypervisor's enabled/disabled status or
  forced-down flag differs from the baseline. Re-enabling a host that was out
  for maintenance, or forcing a live host down, both fail.
- ``pets_preserved``: every pet server still exists under its original ID.
- ``pets_not_moved``: every pet is on its baseline host (no evacuation or
  migration away from a host that was never dead).
- ``pets_settled``: no pet stays in ERROR or a transitional task state.
- ``tenant_slo``: tenant operations in the window meet the scenario's SLO.
- ``objects_durable``: every pre-incident object and every write the region
  acknowledged reads back with its original content.
- ``object_ring_capacity``: every baseline object-ring device is back with at
  least its baseline weight.
"""

from __future__ import annotations

import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.apps.openstack.application import WORKLOAD_COMPLETION_TIMEOUT_S
from sregym.service.apps.openstack.ledger import summarize, violations

INVARIANTS = (
    "objects_durable",
    "object_ring_capacity",
    "hypervisors_up",
    "hypervisor_status_as_baseline",
    "pets_preserved",
    "pets_not_moved",
    "pets_settled",
    "tenant_slo",
)

# When several invariants fail, report the one that matters most: lost or moved
# customer state outranks availability, which outranks residual degradation.
PRECEDENCE = (
    "objects_durable",
    "pets_preserved",
    "pets_not_moved",
    "object_ring_capacity",
    "hypervisors_up",
    "hypervisor_status_as_baseline",
    "pets_settled",
    "tenant_slo",
)

OBJECT_INVARIANTS = {"objects_durable", "object_ring_capacity"}

REASONS = {
    "objects_durable": "objects_lost",
    "object_ring_capacity": "object_capacity_not_restored",
    "pets_preserved": "pets_missing",
    "pets_not_moved": "pets_moved",
    "hypervisors_up": "hypervisors_down",
    "hypervisor_status_as_baseline": "hypervisor_status_changed",
    "pets_settled": "pets_unsettled",
    "tenant_slo": "tenant_slo_violated",
}


def object_problems(baseline_devices: list, audit: dict) -> dict[str, list[str]]:
    lost = [
        f"{tenant}/{entry}"
        for tenant, report in sorted(audit["tenants"].items())
        for entry in [*report["missing"], *(f"{name}: content changed" for name in report["corrupt"])]
    ]
    current = {device: weight for device, weight in audit["ring_devices"]}
    reduced = [
        f"{device}: weight {current.get(device, 0)} < {weight}"
        for device, weight in baseline_devices
        if current.get(device, 0) < weight
    ]
    return {"objects_durable": lost, "object_ring_capacity": reduced}


def acknowledged_writes(records: list[dict]) -> dict[str, list[tuple[str, str]]]:
    writes: dict[str, list[tuple[str, str]]] = {}
    for record in records:
        if record["op"] == "object_put" and record["ok"]:
            writes.setdefault(record["tenant"], []).append((record["object"], record["sha256"]))
    return writes


def fleet_state(services: list[dict]) -> dict[str, dict]:
    return {
        s["Host"]: {
            "state": s["State"],
            "status": s["Status"],
            "forced_down": bool(s.get("Forced Down")),
            "reason": s.get("Disabled Reason"),
        }
        for s in services
    }


def fleet_problems(baseline: dict[str, dict], current: dict[str, dict]) -> dict[str, list[str]]:
    down, changed = [], []
    for host, expected in baseline.items():
        actual = current.get(host)
        if actual is None or actual["state"] != "up":
            down.append(host)
        if actual is not None and (
            actual["status"] != expected["status"] or actual["forced_down"] != expected["forced_down"]
        ):
            changed.append(f"{host}: {expected['status']}->{actual['status']} forced_down={actual['forced_down']}")
    return {"hypervisors_up": down, "hypervisor_status_as_baseline": changed}


def pet_problems(baseline: dict[str, dict], samples: list[dict[str, dict]]) -> dict[str, list[str]]:
    """Compare pets against the baseline. A pet is unsettled only if it is
    unsettled in every sample, so a tenant's ordinary reboot in flight at one
    sample does not count."""
    final = samples[-1]
    missing = sorted(baseline[p]["name"] for p in baseline if p not in final)
    moved = sorted(
        f"{baseline[p]['name']}: {baseline[p]['host']}->{final[p]['host']}"
        for p in baseline
        if p in final and final[p]["host"] != baseline[p]["host"]
    )

    def unsettled(pet):
        return pet["status"] != "ACTIVE" or bool(pet["task_state"])

    stuck = sorted(
        f"{baseline[p]['name']}: status={final[p]['status']} task_state={final[p]['task_state']}"
        for p in baseline
        if p in final
        and (final[p]["status"] == "ERROR" or all(p in sample and unsettled(sample[p]) for sample in samples))
    )
    return {"pets_preserved": missing, "pets_not_moved": moved, "pets_settled": stuck}


class OpenStackRegionRecoveryOracle(Oracle):
    evaluation_timeout_seconds = 900
    FAILURE_CLASSES = {
        "pets_missing": FailureClass.AGENT_ERROR,
        "pets_moved": FailureClass.AGENT_ERROR,
        "hypervisors_down": FailureClass.AGENT_ERROR,
        "hypervisor_status_changed": FailureClass.AGENT_ERROR,
        "pets_unsettled": FailureClass.AGENT_ERROR,
        "tenant_slo_violated": FailureClass.AGENT_ERROR,
        "objects_lost": FailureClass.AGENT_ERROR,
        "object_capacity_not_restored": FailureClass.AGENT_ERROR,
        # The ledger lives outside the incident namespace; losing it is not the agent's doing.
        "tenant_ledger_unavailable": FailureClass.ENVIRONMENT_ERROR,
        "baseline_missing": FailureClass.HARNESS_ERROR,
    }

    def __init__(self, problem, scenario):
        super().__init__(problem)
        self.scenario = scenario
        unknown = set(scenario.invariants) - set(INVARIANTS)
        if unknown:
            raise ValueError(f"Unknown invariants in {scenario.id}: {sorted(unknown)}")
        self.invariants = scenario.invariants
        grading = scenario.grading
        self.soak_s = float(grading.get("soak_s", 30))
        self.window_s = float(grading.get("window_s", 150))
        self.sample_interval_s = float(grading.get("sample_interval_s", 25))
        self.slo = scenario.slo()
        self.baseline: dict | None = None

    @property
    def app(self):
        return self.problem.app

    def services(self) -> dict[str, dict]:
        return fleet_state(self.app.compute_services(long=True))

    def pets(self) -> dict[str, dict]:
        return {p["id"]: p for p in self.app.pets()}

    def capture_baseline(self) -> None:
        app = self.app
        services = self.services()
        expected = set(app.tier.hypervisors)
        missing = sorted(expected - {h for h, s in services.items() if s["state"] == "up"})
        if missing:
            raise RuntimeError(f"Hypervisors are not up before the fault: {missing}")
        # Tenants reboot their pets all the time; a pet counts as settled if any
        # of a few samples shows it idle, the same rule grading uses.
        samples = [self.pets()]
        for _ in range(2):
            time.sleep(10)
            samples.append(self.pets())
        pets = samples[-1]
        unsettled = pet_problems(pets, samples)["pets_settled"]
        if len(pets) != app.tier.pets or unsettled:
            raise RuntimeError(f"Expected {app.tier.pets} settled pets before the fault; unsettled: {unsettled}")
        status = app.workload_status()
        until = time.time() - WORKLOAD_COMPLETION_TIMEOUT_S
        since = until - self.window_s
        if status["started"] > since:
            raise RuntimeError("The tenant workload has not run for a full baseline window")
        summary = summarize(app.ledger(since, until)["records"], since, until)
        problems = violations(summary, self.slo)
        if problems:
            raise RuntimeError(f"The tenant SLO is not met before the fault: {problems}")
        ring_devices = None
        if OBJECT_INVARIANTS & set(self.invariants):
            audit = app.object_audit({})
            lost = object_problems([], audit)["objects_durable"]
            if lost:
                raise RuntimeError(f"Objects are unreadable before the fault: {lost[:10]}")
            ring_devices = audit["ring_devices"]
        self.baseline = {
            "services": {h: s for h, s in services.items() if h in expected},
            "pets": pets,
            "episode": status["episode"],
            "tenant_slo": summary,
            "ring_devices": ring_devices,
        }
        print(f"[OpenStack] baseline: {len(expected)} hypervisors up, {len(pets)} pets, tenant SLO met")

    def evaluate(self, *args, **kwargs) -> dict:
        print("== OpenStack region recovery ==")
        if self.baseline is None:
            return self.fail("baseline_missing")
        try:
            time.sleep(self.soak_s)
            start = time.time()
            fleet_samples, pet_samples = [], []
            while True:
                fleet_samples.append(fleet_problems(self.baseline["services"], self.services()))
                pet_samples.append(self.pets())
                if time.time() + self.sample_interval_s > start + self.window_s:
                    break
                time.sleep(self.sample_interval_s)
            end = max(time.time(), start + self.window_s)
            time.sleep(max(0.0, end - time.time()) + WORKLOAD_COMPLETION_TIMEOUT_S)
            pet_samples.append(self.pets())
            try:
                ledger = self.app.ledger(start, end)
            except Exception as exc:
                return self.fail("tenant_ledger_unavailable", error=str(exc))
            if ledger.get("episode") != self.baseline["episode"]:
                return self.fail("tenant_ledger_unavailable", error="the tenant workload restarted during the incident")
            audit = None
            if OBJECT_INVARIANTS & set(self.invariants):
                # Every write acknowledged since the workload started, not just in the window.
                audit = self.app.object_audit(acknowledged_writes(self.app.ledger(0, end)["records"]))
        except Exception as exc:
            return self.fail_from_exception(exc)

        summary = summarize(ledger["records"], start, end)
        results: dict[str, list[str]] = {name: [] for name in INVARIANTS}
        for sample in fleet_samples:
            for name, problems in sample.items():
                results[name] += [p for p in problems if p not in results[name]]
        results.update(pet_problems(self.baseline["pets"], pet_samples))
        results["tenant_slo"] = violations(summary, self.slo)
        if audit is not None:
            results.update(object_problems(self.baseline["ring_devices"], audit))

        detail = {
            "window": {"start": start, "end": end, "samples": len(fleet_samples)},
            "invariants": {name: results[name] for name in self.invariants},
            "tenant_slo": summary["by_op"],
        }
        for name in self.invariants:
            print(f"{'❌' if results[name] else '✅'} {name}: {results[name] or 'ok'}")
        failed = next((name for name in PRECEDENCE if name in self.invariants and results[name]), None)
        if failed:
            return self.fail(REASONS[failed], **detail)
        return {"success": True, **detail}
