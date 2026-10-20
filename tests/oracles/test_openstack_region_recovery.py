import pytest

from sregym.conductor.oracles.apps.openstack import region_recovery
from sregym.conductor.oracles.apps.openstack.region_recovery import (
    OpenStackRegionRecoveryOracle,
    fleet_problems,
    pet_problems,
)
from sregym.conductor.problems.apps.openstack import scenario as scenarios
from sregym.service.apps.openstack.tiers import TIERS

TIER = TIERS["small"]
SCENARIO = scenarios.load("host_access_revocation", known_invariants=set(region_recovery.INVARIANTS))


def _service(host, state="up", status="enabled", forced=False):
    return {"Host": host, "State": state, "Status": status, "Forced Down": forced, "Disabled Reason": None}


def _pet(index, host, status="ACTIVE", task_state=None):
    return {"id": f"id-{index}", "name": f"pet-{index}", "status": status, "task_state": task_state, "host": host}


def _records(start, ok=True, tenants=("tenant-01", "tenant-02")):
    out = []
    for i in range(12):
        for tenant in tenants:
            for op in ("boot", "delete", "pet_reboot", "list"):
                t = start + i
                out.append(
                    {
                        "t": t,
                        "end": t + 1,
                        "tenant": tenant,
                        "op": op,
                        "ok": ok,
                        "latency_s": 1,
                        "error": None if ok else "x",
                    }
                )
    return out


class FakeRegion:
    def __init__(self):
        self.tier = TIER
        self.services = [_service(h) for h in TIER.hypervisors]
        self.pet_list = [_pet(i, TIER.hypervisors[i % 6]) for i in range(TIER.pets)]
        self.healthy_ledger = True
        self.episode = "e1"

    def compute_services(self, long=False):
        return self.services

    def pets(self):
        return list(self.pet_list)

    def workload_status(self):
        return {"episode": self.episode, "started": 0.0, "latest_end": 1e12, "records": 1}

    def ledger(self, since, until=None):
        return {"episode": self.episode, "records": _records(since, ok=self.healthy_ledger)}


class FakeProblem:
    def __init__(self):
        self.app = FakeRegion()


@pytest.fixture
def oracle(monkeypatch):
    monkeypatch.setattr(region_recovery.time, "sleep", lambda s: None)
    monkeypatch.setattr(region_recovery, "WORKLOAD_COMPLETION_TIMEOUT_S", 0)
    problem = FakeProblem()
    oracle = OpenStackRegionRecoveryOracle(problem, SCENARIO)
    # Sleeping is patched out, so the window is one sample and the fake ledger
    # supplies twelve operations of each kind per tenant inside it.
    oracle.window_s = 12
    oracle.sample_interval_s = 100
    oracle.capture_baseline()
    return oracle


def test_healthy_region_passes(oracle):
    assert oracle.evaluate()["success"] is True


def test_down_hypervisor_fails(oracle):
    oracle.app.services[-1]["State"] = "down"
    verdict = oracle.evaluate()
    assert verdict["reason"] == "hypervisors_down"
    assert verdict["failure_class"] == "agent_error"


def test_enabling_a_maintenance_host_is_a_status_change():
    baseline = {"hv-r1-1": {"state": "up", "status": "disabled", "forced_down": False}}
    current = {"hv-r1-1": {"state": "up", "status": "enabled", "forced_down": False}}
    assert fleet_problems(baseline, current)["hypervisor_status_as_baseline"]


def test_forcing_a_live_host_down_is_a_status_change():
    baseline = {"hv-r3-0": {"state": "up", "status": "enabled", "forced_down": False}}
    # A forced-down service also reports down; both invariants must see it.
    current = {"hv-r3-0": {"state": "down", "status": "enabled", "forced_down": True}}
    problems = fleet_problems(baseline, current)
    assert problems["hypervisors_up"] == ["hv-r3-0"]
    assert problems["hypervisor_status_as_baseline"]


def test_evacuated_pet_outranks_other_failures(oracle):
    oracle.app.pet_list[0] = {**oracle.app.pet_list[0], "host": "hv-r1-1"}
    oracle.app.healthy_ledger = False
    assert oracle.evaluate()["reason"] == "pets_moved"


def test_recreated_pet_counts_as_missing(oracle):
    oracle.app.pet_list[0] = {**oracle.app.pet_list[0], "id": "new-id"}
    assert oracle.evaluate()["reason"] == "pets_missing"


def test_pet_rebooting_in_one_sample_is_settled_but_stuck_in_all_is_not():
    baseline = {"a": _pet(0, "hv-r1-0") | {"id": "a"}}
    busy = {"a": {**baseline["a"], "status": "REBOOT", "task_state": "rebooting"}}
    idle = {"a": baseline["a"]}
    assert pet_problems(baseline, [busy, idle])["pets_settled"] == []
    assert pet_problems(baseline, [busy, busy])["pets_settled"]


def test_unmet_tenant_slo_fails(oracle):
    oracle.app.healthy_ledger = False
    assert oracle.evaluate()["reason"] == "tenant_slo_violated"


def test_restarted_workload_is_an_environment_failure(oracle):
    oracle.app.episode = "e2"
    verdict = oracle.evaluate()
    assert verdict["reason"] == "tenant_ledger_unavailable"
    assert verdict["failure_class"] == "environment_error"


def test_unhealthy_baseline_is_refused(monkeypatch):
    monkeypatch.setattr(region_recovery, "WORKLOAD_COMPLETION_TIMEOUT_S", 0)
    problem = FakeProblem()
    problem.app.services[0]["State"] = "down"
    with pytest.raises(RuntimeError, match="not up before the fault"):
        OpenStackRegionRecoveryOracle(problem, SCENARIO).capture_baseline()


def test_lost_or_changed_objects_and_reduced_ring_are_reported():
    audit = {
        "ring_devices": [["s0/d0", 100.0], ["s1/d0", 50.0]],
        "tenants": {"tenant-01": {"checked": 3, "missing": ["canary-2: HTTP 404"], "corrupt": ["write-a"]}},
    }
    problems = region_recovery.object_problems([["s0/d0", 100.0], ["s1/d0", 100.0], ["s2/d1", 100.0]], audit)
    assert problems["objects_durable"] == ["tenant-01/canary-2: HTTP 404", "tenant-01/write-a: content changed"]
    assert problems["object_ring_capacity"] == ["s1/d0: weight 50.0 < 100.0", "s2/d1: weight 0 < 100.0"]


def test_only_acknowledged_writes_must_survive():
    records = [
        {"op": "object_put", "ok": True, "tenant": "t1", "object": "write-a", "sha256": "aa"},
        {"op": "object_put", "ok": False, "tenant": "t1", "object": "write-b", "error": "HTTP 503"},
        {"op": "object_get", "ok": True, "tenant": "t1", "object": "canary-0"},
    ]
    assert region_recovery.acknowledged_writes(records) == {"t1": [("write-a", "aa")]}
