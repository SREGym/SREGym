"""Require capacity that survives the automation, not just a manual scale-up.

A responder who scales the Deployment up while the latency is still there has
that work reverted at the next capacity decision, so capacity is observed over a
window spanning several of them rather than sampled once. The window also has to
tell three states apart -- capacity still coming up, capacity held, and capacity
taken away again -- because a correct fix graded seconds after `kubectl scale` is
still waiting for its new pod, and that is rollout lag, not lost capacity.
"""

import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.saas import SaaSOracle


def latency_budget(baseline_p50_ms):
    """A generous multiple of measured healthy latency, plus absolute headroom.

    Calibrated rather than absolute: healthy latency here depends on how fast the
    host is, while the injected delay is an order of magnitude larger.
    """
    return max(500.0, baseline_p50_ms * 5 + 200)


class MattermostCascadeOracle(SaaSOracle):
    FAILURE_CLASSES = {
        "gateway_capacity_below_floor": FailureClass.AGENT_ERROR,
        "capacity_automation_still_shrinking": FailureClass.AGENT_ERROR,
        "gateway_shedding_requests": FailureClass.AGENT_ERROR,
        "gateway_latency_unresolved": FailureClass.AGENT_ERROR,
        "gateway_missing": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.baseline_latency_ms = None

    @property
    def stability_seconds(self):
        """Long enough for the automation to act at least twice."""
        return 2 * self.problem.app.scaler_interval + 10

    @property
    def readiness_grace_seconds(self):
        """Bounded time for a legitimate scale-up's pods to become ready.

        Without this, a correct fix graded a few seconds after `kubectl scale`
        fails on `ready < floor` while its new pod is still starting -- which is
        rollout lag, not lost capacity.
        """
        return 60

    def observe_capacity(self, floor):
        """Distinguish capacity coming up, held, and taken away again.

        Returns ``(reason, samples)`` with ``reason`` None when capacity reached
        the floor and stayed there for the whole stability window. Reaching the
        floor and then dropping is the automation undoing the fix; never reaching
        it is capacity that was not restored at all. Sampling once cannot tell
        those apart, and neither can a single up-front readiness check.
        """
        app = self.problem.app
        samples = []
        reached = False
        held_since = None
        deadline = time.monotonic() + self.readiness_grace_seconds + self.stability_seconds
        while time.monotonic() < deadline:
            desired, ready = app.gateway_replicas()
            samples.append({"desired": desired, "ready": ready})
            if desired >= floor and ready >= floor:
                reached = True
                held_since = held_since or time.monotonic()
                if time.monotonic() - held_since >= self.stability_seconds:
                    return None, samples
            elif reached:
                return "capacity_automation_still_shrinking", samples
            else:
                held_since = None
            time.sleep(5)
        return ("capacity_automation_still_shrinking" if reached else "gateway_capacity_below_floor"), samples

    def capture_baseline(self):
        super().capture_baseline()
        # Healthy latency through the gateway, so the verdict is not tied to an
        # absolute number that depends on this host's speed.
        self.baseline_latency_ms = self.problem.app.probe_through_gateway()["p50_ms"]

    def evaluate(self):
        app = self.problem.app
        if not self.baseline or self.baseline_latency_ms is None:
            return super().evaluate()
        try:
            if not app.command("get", "deployment", "chat-gateway", "--ignore-not-found", "-o", "name").strip():
                # Deleting the gateway removes the symptom by removing the service.
                return self.fail("gateway_missing")

            floor = app.capacity_floor
            # Watch across more than one automation decision interval. A manual
            # scale-up that the policy undoes shows up here and nowhere else.
            shortfall, observed = self.observe_capacity(floor)
            if shortfall:
                return self.fail(
                    shortfall,
                    floor=floor,
                    observed=observed,
                    recent_decisions=app.scaler_decisions()[-5:],
                )

            customer = app.probe_through_gateway()
            if customer["shed"]:
                return self.fail("gateway_shedding_requests", customer=customer)
            budget = latency_budget(self.baseline_latency_ms)
            if customer["p50_ms"] > budget:
                return self.fail(
                    "gateway_latency_unresolved",
                    customer=customer,
                    budget_ms=round(budget, 1),
                    healthy_p50_ms=self.baseline_latency_ms,
                )

            result = super().evaluate()
            if not result.get("success"):
                return result
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            **result,
            "gateway_replicas": observed[-1] if observed else None,
            "capacity_floor": app.capacity_floor,
            "capacity_held_for_seconds": self.stability_seconds,
            "customer_latency_p50_ms": customer["p50_ms"],
            "requests_shed": 0,
        }
