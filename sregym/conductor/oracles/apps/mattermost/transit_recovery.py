"""Grade the transit cascade in two phases, the way the outage ended.

The responder cannot fix the provider network, so the repair is judged first
while the network is still degraded -- did the service get from down to
degraded? -- and then after the provider restores capacity, when the question
is whether what the responder left behind lets the fleet run normally: capacity
held at its floor, customers served, and automation that still defends the
floor when instances are lost. A pinned replica count with the automation
deleted looks like a fix in the first phase and fails the second.

Verdicts are pure functions of observations so they can be tested without a
cluster; ``evaluate`` only gathers the observations and drives the phases.
"""

import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.postgres_application import PostgresApplicationOracle

CUSTOMER_COUNTERS = ("sent", "served", "shed", "failed", "abandoned", "latency_ms_total")
#: Fraction of what a full fleet could serve over the impaired network that
#: customers must get while it is impaired. The impaired network slows every
#: request rather than capping throughput, so service is set by how many gateway
#: workers are in service to wait on it.
DEGRADED_SERVICE_FRACTION = 0.8
#: Served ratio required once the network has capacity again.
RECOVERED_SERVED_RATIO = 0.98
#: Every request the gateway serves crosses the provider network; well under
#: one forwarded connection per served request means traffic went around it.
MIN_TRANSIT_SHARE = 0.8


def delta(before, after, keys=CUSTOMER_COUNTERS):
    return {key: after.get(key, 0) - before.get(key, 0) for key in keys}


def served_ratio(customers):
    return customers["served"] / customers["sent"] if customers["sent"] else 0.0


def mean_latency_ms(customers):
    return customers["latency_ms_total"] / customers["served"] if customers["served"] else None


def latency_budget(baseline_ms):
    """A generous multiple of measured healthy latency, plus absolute headroom."""
    return max(500.0, baseline_ms * 5 + 200)


def capacity_shortfall(samples, floor, grace_seconds):
    """Samples after the grace period whose in-service count is under the floor."""
    return [s for s in samples if s["t"] >= grace_seconds and s["in_service"] < floor]


def stuck_provisioning(samples):
    """Instances stayed in provisioning through the whole second half of the window."""
    tail = samples[len(samples) // 2 :]
    return bool(tail) and all(s["provisioning"] > 0 for s in tail)


def degraded_verdict(samples, customers, forwarded, provisioner_errors, floor, required_ratio, grace_seconds):
    """Phase one, provider network still degraded. Returns ``(reason, detail)``."""
    detail = {
        "served_ratio": round(served_ratio(customers), 3),
        "required_served_ratio": round(required_ratio, 3),
        "customers": customers,
        "transit_forwarded": forwarded,
        "provisioner_open_files_errors": provisioner_errors,
        "fleet": samples[-6:],
    }
    if customers["served"] and forwarded < MIN_TRANSIT_SHARE * customers["served"]:
        return "provider_network_bypassed", detail
    if served_ratio(customers) < required_ratio:
        return "customers_not_served_while_degraded", detail
    if capacity_shortfall(samples, floor, grace_seconds):
        return "gateway_capacity_below_floor", detail
    if stuck_provisioning(samples):
        return "gateway_instances_stuck_provisioning", detail
    return None, detail


def recovered_verdict(samples, customers, floor, budget_ms):
    """Phase two, provider capacity restored, normal traffic."""
    latency = mean_latency_ms(customers)
    detail = {
        "served_ratio": round(served_ratio(customers), 3),
        "mean_latency_ms": None if latency is None else round(latency, 1),
        "latency_budget_ms": round(budget_ms, 1),
        "customers": customers,
        "fleet": samples[-6:],
    }
    if capacity_shortfall(samples, floor, grace_seconds=0):
        return "gateway_capacity_below_floor_after_recovery", detail
    if stuck_provisioning(samples):
        return "gateway_instances_stuck_provisioning", detail
    if served_ratio(customers) < RECOVERED_SERVED_RATIO:
        return "customers_not_served_after_recovery", detail
    if latency is None or latency > budget_ms:
        return "gateway_latency_unresolved", detail
    return None, detail


def challenge_verdict(samples, floor, hold_seconds):
    """Instance loss: in-service capacity must come back to the floor and stay.

    Nothing but the capacity automation restores a Deployment's replica count, so
    a pinned count with the automation deleted, disabled, or allowed to sit under
    the floor never gets back.
    """
    held_since = None
    for sample in samples:
        if sample["in_service"] >= floor and sample["requested"] >= floor:
            held_since = sample["t"] if held_since is None else held_since
            if sample["t"] - held_since >= hold_seconds:
                return None, {"restored_after_seconds": held_since, "fleet": samples[-6:]}
        else:
            held_since = None
    if stuck_provisioning(samples) and samples[-1]["requested"] >= floor:
        return "replacement_instances_stuck_provisioning", {"fleet": samples[-6:]}
    return "capacity_not_restored_by_automation", {"fleet": samples[-6:]}


class MattermostTransitOracle(PostgresApplicationOracle):
    evaluation_timeout_seconds = 1800
    FAILURE_CLASSES = {
        "gateway_missing": FailureClass.AGENT_ERROR,
        "provider_network_bypassed": FailureClass.AGENT_ERROR,
        "gateway_instances_stuck_provisioning": FailureClass.AGENT_ERROR,
        "gateway_capacity_below_floor": FailureClass.AGENT_ERROR,
        "customers_not_served_while_degraded": FailureClass.AGENT_ERROR,
        "gateway_capacity_below_floor_after_recovery": FailureClass.AGENT_ERROR,
        "customers_not_served_after_recovery": FailureClass.AGENT_ERROR,
        "gateway_latency_unresolved": FailureClass.AGENT_ERROR,
        "capacity_not_restored_by_automation": FailureClass.AGENT_ERROR,
        "replacement_instances_stuck_provisioning": FailureClass.AGENT_ERROR,
    }
    sample_seconds = 5
    #: Time for capacity the responder just requested to come into service.
    grace_seconds = 60
    #: Time for the provider's capacity increase to reach live connections.
    settle_seconds = 60
    #: Instance loss: the gateway is cut to this many replicas.
    challenge_replicas = 1

    def __init__(self, problem):
        super().__init__(problem)
        self.baseline_latency_ms = None

    @property
    def window_seconds(self):
        """Several capacity decisions, including a full scale-in stabilization."""
        app = self.problem.app
        return app.scaler_stabilization_seconds + 4 * app.scaler_interval + 30

    @property
    def challenge_seconds(self):
        """Policy bounds apply on the next decision; then the new pods provision."""
        return 3 * self.problem.app.scaler_interval + 120

    @property
    def hold_seconds(self):
        return 2 * self.problem.app.scaler_interval

    def capture_baseline(self):
        super().capture_baseline()
        app = self.problem.app
        before = app.customer_stats()
        time.sleep(30)
        healthy = delta(before, app.customer_stats())
        latency = mean_latency_ms(healthy)
        if latency is None or served_ratio(healthy) < RECOVERED_SERVED_RATIO:
            raise RuntimeError(f"Customer traffic is not healthy before the incident: {healthy}")
        self.baseline_latency_ms = latency

    def watch(self, seconds):
        """Fleet samples plus customer and transit counters across a window."""
        app = self.problem.app
        customers_before, transit_before = app.customer_stats(), app.transit_stats()
        samples, started = [], time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            samples.append({"t": round(elapsed, 1), **app.gateway_fleet()})
            if elapsed >= seconds:
                break
            time.sleep(self.sample_seconds)
        customers = delta(customers_before, app.customer_stats())
        forwarded = app.transit_stats()["forwarded"] - transit_before["forwarded"]
        return samples, customers, forwarded

    def required_degraded_ratio(self):
        app = self.problem.app
        full_fleet = app.fleet_max * app.gateway_workers / app.transit_impaired_latency
        return DEGRADED_SERVICE_FRACTION * min(1.0, full_fleet / app.surge_rate)

    def evaluate(self):
        app = self.problem.app
        if not self.baseline or self.baseline_latency_ms is None:
            return super().evaluate()
        phases = {}
        try:
            if not app.command("get", "deployment", "chat-gateway", "--ignore-not-found", "-o", "name").strip():
                return self.fail("gateway_missing")

            # Phase one: the network the responder cannot touch is still degraded.
            app.set_transit_capacity(app.transit_capacity)
            app.set_customer_rate(app.surge_rate)
            samples, customers, forwarded = self.watch(self.grace_seconds + self.window_seconds)
            floor = app.capacity_floor
            reason, phases["degraded"] = degraded_verdict(
                samples,
                customers,
                forwarded,
                app.provisioner_errors(since_seconds=self.window_seconds),
                floor,
                self.required_degraded_ratio(),
                self.grace_seconds,
            )
            if reason:
                return self.fail(reason, phase="degraded", **phases)

            # The provider restores capacity; traffic returns to normal.
            app.set_transit_capacity(app.transit_capacity_fixed)
            app.set_customer_rate(app.healthy_rate)
            time.sleep(self.settle_seconds)

            samples, customers, _ = self.watch(self.window_seconds)
            reason, phases["recovered"] = recovered_verdict(
                samples, customers, floor, latency_budget(self.baseline_latency_ms)
            )
            if reason:
                return self.fail(reason, phase="recovered", **phases)

            if failure := self.instance_loss_at_floor(phases):
                return failure

            app.command("rollout", "status", "deployment/chat-gateway", "--timeout=300s", timeout=330)
            result = super().evaluate()
            if not result.get("success"):
                return result
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {**result, **phases, "capacity_floor": floor}

    def instance_loss_at_floor(self, phases):
        """Instance loss after recovery: only the automation can restore capacity."""
        app = self.problem.app
        app.command("scale", "deployment/chat-gateway", f"--replicas={self.challenge_replicas}")
        samples, _, _ = self.watch(self.challenge_seconds)
        reason, phases["instance_loss"] = challenge_verdict(samples, app.capacity_floor, self.hold_seconds)
        if reason:
            return self.fail(reason, phase="instance_loss", decisions=app.scaler_decisions()[-5:], **phases)
        return None
