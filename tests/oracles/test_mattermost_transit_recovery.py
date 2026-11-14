"""The grading contract for the transit cascade (Slack, 2021-01-04).

The repair is judged twice: while the provider network is still degraded, and
after the provider restores it. A fix that only looks right in the first phase
-- a pinned replica count with the automation deleted -- must fail the second.
"""

from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.apps.mattermost import transit_recovery as grading
from sregym.conductor.oracles.apps.mattermost.transit_recovery import (
    MattermostTransitOracle,
    challenge_verdict,
    degraded_verdict,
    recovered_verdict,
)
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.postgres_application import PostgresApplicationOracle

FLOOR = 3


def fleet(t, in_service, requested=None, provisioning=0):
    return {
        "t": t,
        "requested": in_service if requested is None else requested,
        "in_service": in_service,
        "provisioning": provisioning,
    }


def customers(sent=1000, served=800, latency_ms=50.0):
    return {
        "sent": sent,
        "served": served,
        "shed": 0,
        "failed": sent - served,
        "abandoned": 0,
        "latency_ms_total": served * latency_ms,
    }


def degraded(samples=None, traffic=None, forwarded=None, errors=0, required=0.6):
    traffic = traffic or customers()
    samples = samples or [fleet(t, 9) for t in range(0, 200, 5)]
    return degraded_verdict(
        samples,
        traffic,
        traffic["served"] if forwarded is None else forwarded,
        errors,
        FLOOR,
        required,
        grace_seconds=60,
    )[0]


# ---- phase one: provider network still degraded ------------------------


def test_degraded_but_serving_up_to_the_network_passes():
    assert degraded() is None


def test_routing_around_the_provider_network_fails():
    assert degraded(forwarded=10) == "provider_network_bypassed"


def test_open_files_errors_are_recorded_not_graded():
    traffic = customers()
    reason, detail = degraded_verdict(
        [fleet(t, 9) for t in range(0, 200, 5)], traffic, traffic["served"], 4, FLOOR, 0.6, grace_seconds=60
    )
    assert reason is None
    assert detail["provisioner_open_files_errors"] == 4


def test_customers_are_the_first_evidence():
    stuck = [fleet(t, 3, requested=12, provisioning=9) for t in range(0, 200, 5)]
    assert degraded(samples=stuck, traffic=customers(served=300)) == "customers_not_served_while_degraded"


def test_instances_stuck_in_provisioning_fail():
    samples = [fleet(t, 3, requested=12, provisioning=9) for t in range(0, 200, 5)]
    assert degraded(samples=samples) == "gateway_instances_stuck_provisioning"


def test_provisioning_that_drains_during_the_window_is_not_stuck():
    samples = [fleet(t, 3 + t // 20, requested=12, provisioning=max(0, 9 - t // 20)) for t in range(0, 200, 5)]
    assert grading.stuck_provisioning(samples) is False


def test_capacity_under_the_floor_after_grace_fails_but_not_during_it():
    early_dip = [fleet(0, 1), *[fleet(t, 6) for t in range(5, 200, 5)]]
    assert degraded(samples=early_dip) is None
    late_dip = [*[fleet(t, 6) for t in range(0, 150, 5)], fleet(150, 2), fleet(155, 6)]
    assert degraded(samples=late_dip) == "gateway_capacity_below_floor"


def test_gateway_still_the_bottleneck_fails():
    assert degraded(traffic=customers(served=300)) == "customers_not_served_while_degraded"


def test_required_ratio_is_relative_to_what_a_full_fleet_could_serve():
    """Each request holds a gateway worker for the impaired network's latency."""
    app = SimpleNamespace(
        fleet_max=8, instance_quota=None, gateway_workers=8, transit_impaired_latency=0.8, surge_rate=140
    )
    oracle = MattermostTransitOracle.__new__(MattermostTransitOracle)
    oracle.problem = SimpleNamespace(app=app)
    fraction = grading.DEGRADED_SERVICE_FRACTION
    assert oracle.required_degraded_ratio() == pytest.approx(fraction * 64 / (0.8 * 140))
    app.fleet_max = 16
    assert oracle.required_degraded_ratio() == pytest.approx(fraction)


# ---- phase two: provider capacity restored -----------------------------


def test_recovered_fleet_serving_normally_passes():
    samples = [fleet(t, 3) for t in range(0, 120, 5)]
    assert recovered_verdict(samples, customers(served=995), FLOOR, 500)[0] is None


def test_recovery_checks_floor_latency_and_service():
    healthy = [fleet(t, 3) for t in range(0, 120, 5)]
    assert recovered_verdict([*healthy, fleet(120, 2)], customers(served=995), FLOOR, 500)[0] == (
        "gateway_capacity_below_floor_after_recovery"
    )
    assert recovered_verdict(healthy, customers(served=900), FLOOR, 500)[0] == "customers_not_served_after_recovery"
    slow = customers(served=995, latency_ms=900)
    assert recovered_verdict(healthy, slow, FLOOR, 500)[0] == "gateway_latency_unresolved"
    assert recovered_verdict(healthy, customers(sent=0, served=0), FLOOR, 500)[0] == (
        "customers_not_served_after_recovery"
    )


# ---- instance loss after recovery --------------------------------------


def test_automation_that_restores_the_floor_passes():
    samples = [fleet(0, 1), fleet(15, 1, requested=3), fleet(30, 3), fleet(45, 3), fleet(60, 3)]
    reason, detail = challenge_verdict(samples, FLOOR, hold_seconds=30)
    assert reason is None and detail["restored_after_seconds"] == 30


@pytest.mark.parametrize(
    "samples",
    [
        # Automation deleted or disabled: nothing raises the count again.
        [fleet(t, 1) for t in range(0, 165, 5)],
        # Policy min left at 1: the count is never raised to the floor.
        [fleet(t, 1, requested=1) for t in range(0, 165, 5)],
        # Reached the floor, then the automation took it away again.
        [fleet(0, 1), fleet(15, 3), fleet(30, 3), fleet(40, 2), fleet(50, 2)],
    ],
)
def test_capacity_not_defended_by_automation_fails(samples):
    assert challenge_verdict(samples, FLOOR, hold_seconds=30)[0] == "capacity_not_restored_by_automation"


def test_replacements_requested_but_stuck_provisioning_fail_on_their_own_reason():
    """The automation did its part; the new instances never came into service."""
    samples = [fleet(t, 1, requested=3, provisioning=2) for t in range(0, 165, 5)]
    assert challenge_verdict(samples, FLOOR, hold_seconds=30)[0] == "replacement_instances_stuck_provisioning"


def test_every_reason_is_an_agent_error():
    assert set(MattermostTransitOracle.FAILURE_CLASSES.values()) == {FailureClass.AGENT_ERROR}


# ---- evaluate drives the phases in order -------------------------------


class FakeApp:
    """Counters move as a scripted world would; records what the oracle did."""

    capacity_floor = FLOOR
    scaler_interval = 15
    scaler_stabilization_seconds = 45
    transit_capacity = 80
    transit_capacity_fixed = 1000
    transit_impaired_latency = 0.8
    fleet_max = 16
    gateway_workers = 8
    surge_rate = 100
    healthy_rate = 60
    instance_quota = None
    states_capacity_requirement = True

    def __init__(self, world):
        self.world = world
        self.actions = []
        self.phase = "degraded"
        self.customers = {key: 0 for key in grading.CUSTOMER_COUNTERS}
        self.forwarded = 0
        self.replicas = 9

    def command(self, *args, **kwargs):
        self.actions.append(args)
        if args[:2] == ("scale", "deployment/chat-gateway"):
            self.replicas = int(args[2].split("=")[1])
            self.phase = "instance_loss"
        if args[:2] == ("get", "deployment"):
            return "deployment.apps/chat-gateway" if self.world.get("gateway", True) else ""
        return ""

    def set_transit_capacity(self, value):
        self.actions.append(("transit", value))
        if value == self.transit_capacity_fixed:
            self.phase = "recovered"

    def set_customer_rate(self, value):
        self.actions.append(("customers", value))

    def customer_stats(self):
        ratio = self.world.get(f"{self.phase}_served", 0.99 if self.phase != "degraded" else 0.9)
        self.customers["sent"] += 100
        self.customers["served"] += int(100 * ratio)
        self.customers["latency_ms_total"] += int(100 * ratio) * 20
        if not self.world.get("bypass"):
            self.forwarded += int(100 * ratio)
        return dict(self.customers)

    def transit_stats(self):
        return {"forwarded": self.forwarded}

    def gateway_fleet(self):
        if self.phase == "instance_loss" and self.world.get("automation", True):
            self.replicas = max(self.replicas, FLOOR)
        return {"requested": self.replicas, "in_service": self.replicas, "provisioning": 0}

    def provisioner_errors(self, since_seconds):
        return self.world.get("provisioner_errors", 0)

    def scaler_decisions(self):
        return []


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(grading.time, "sleep", lambda seconds: None)
    clock = [0.0]

    def monotonic():
        clock[0] += 5
        return clock[0]

    monkeypatch.setattr(grading.time, "monotonic", monotonic)
    monkeypatch.setattr(PostgresApplicationOracle, "evaluate", lambda self: {"success": True})

    def run(**world):
        app = FakeApp(world)
        oracle = MattermostTransitOracle(SimpleNamespace(app=app, namespace="mattermost", kubectl=None))
        oracle.baseline, oracle.baseline_latency_ms = {"token": "ab"}, 20.0
        return oracle.evaluate(), app

    return run


def test_a_durable_repair_passes_all_three_phases(run):
    result, app = run()
    assert result["success"], result
    assert {"degraded", "recovered", "instance_loss"} <= set(result)
    # Degraded first, then the provider's fix, then instance loss.
    order = [a for a in app.actions if a[0] in ("transit", "scale")]
    assert order == [("transit", 80), ("transit", 1000), ("scale", "deployment/chat-gateway", "--replicas=1")]


def test_a_pinned_fleet_without_automation_fails_only_at_instance_loss(run):
    result, _ = run(automation=False)
    assert result["reason"] == "capacity_not_restored_by_automation"
    assert result["detail"]["phase"] == "instance_loss"


@pytest.mark.parametrize(
    ("world", "reason"),
    [
        ({"gateway": False}, "gateway_missing"),
        ({"bypass": True}, "provider_network_bypassed"),
        ({"degraded_served": 0.2}, "customers_not_served_while_degraded"),
        ({"recovered_served": 0.5}, "customers_not_served_after_recovery"),
    ],
)
def test_each_phase_fails_on_its_own_evidence(run, world, reason):
    result, app = run(**world)
    assert result["success"] is False and result["reason"] == reason
    if reason != "customers_not_served_after_recovery":
        # Failing while degraded never restores the network for the agent.
        assert ("transit", 1000) not in app.actions
