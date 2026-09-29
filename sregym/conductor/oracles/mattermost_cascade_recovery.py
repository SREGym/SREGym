"""Require capacity that survives the automation, not just a manual scale-up.

The recovery has two halves and neither alone is enough: remove the latency that
saturates the gateway, and stop the capacity automation reading that saturation
as spare capacity. A responder who only scales the Deployment up has its work
reverted at the next decision, so capacity is observed over a window that spans
several of them rather than sampled once.
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
            desired, ready = app.gateway_replicas()
            if desired < floor or ready < floor:
                return self.fail("gateway_capacity_below_floor", desired=desired, ready=ready, floor=floor)

            # Watch across the automation's decision interval. A manual scale-up
            # that the policy undoes shows up here and nowhere else.
            observed = []
            deadline = time.monotonic() + self.stability_seconds
            while time.monotonic() < deadline:
                time.sleep(5)
                desired, ready = app.gateway_replicas()
                observed.append({"desired": desired, "ready": ready})
                if desired < floor or ready < floor:
                    return self.fail(
                        "capacity_automation_still_shrinking",
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
