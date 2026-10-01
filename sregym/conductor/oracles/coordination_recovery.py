"""Grade a long-horizon recovery on outcome, accumulated cost, and what survives.

Three things are graded that the earlier families did not grade at all:

- **Accumulated cost.** Dropped requests are permanent, so a recovery that got
  there by repeatedly over-admitting is worse than one that did not, even though
  both end up serving. There is a budget, and it is generous but finite.
- **Irreversible damage.** Destroying members to force progress leaves a cluster
  that cannot tolerate another failure. Reaching full service on a cluster with
  no redundancy left is not a recovery.
- **A durable end state.** Capacity is observed across a window longer than the
  cluster's own settling times, so a recovery caught mid-warming or about to
  re-collapse does not pass.
"""

import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.saas import SaaSOracle


def loss_budget(floor_seconds):
    """Requests a careful operator may still lose while finding the sequence.

    Scaled to the incident's own floor rather than fixed: a tier with longer
    settling times gives more opportunity to lose requests before the sequence
    is found. The reference recovery loses none, so this is pure headroom for an
    agent that makes a recoverable mistake, not a licence to over-admit.
    """
    return int(round(floor_seconds * 20))


class CoordinationRecoveryOracle(SaaSOracle):
    FAILURE_CLASSES = {
        "coordination_not_serving": FailureClass.AGENT_ERROR,
        "coordination_leader_unstable": FailureClass.AGENT_ERROR,
        "traffic_not_fully_admitted": FailureClass.AGENT_ERROR,
        "customer_requests_still_failing": FailureClass.AGENT_ERROR,
        "recovery_not_durable": FailureClass.AGENT_ERROR,
        "excessive_requests_dropped": FailureClass.AGENT_ERROR,
        "cluster_redundancy_destroyed": FailureClass.AGENT_ERROR,
        "quorum_permanently_lost": FailureClass.AGENT_ERROR,
        "coordination_service_missing": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.baseline_truth = None

    @property
    def durability_seconds(self):
        """Longer than any single settling time, so mid-flight states fail."""
        stability, warming, step = self.problem.app.settings
        return max(stability, step) + 20

    def capture_baseline(self):
        super().capture_baseline()
        self.baseline_truth = self.problem.app.truth()

    def verdict(self, truth, customer):
        """Name the specific shortfall, most fundamental first."""
        if truth["quorum_lost"]:
            # A one-way door: no later action can restore service.
            return self.fail("quorum_permanently_lost", destroyed=truth["destroyed_members"])
        if truth["destroyed_members"]:
            # Service may be back, but the cluster can no longer lose a member.
            return self.fail("cluster_redundancy_destroyed", destroyed=truth["destroyed_members"])
        if not truth["leader_healthy"]:
            return self.fail(
                "coordination_leader_unstable",
                write_latency_ms=truth["write_latency_ms"],
                latency_budget_ms=truth["latency_budget_ms"],
                watch_subscriptions=truth["watch_subscriptions"],
            )
        if truth["serve_capacity_fraction"] < 1.0:
            return self.fail(
                "coordination_not_serving",
                serve_capacity_fraction=truth["serve_capacity_fraction"],
                compacted=truth["compacted"],
                scheduler_state_fresh=truth["scheduler_state_fresh"],
                cache_warm_fraction=truth["cache_warm_fraction"],
            )
        if truth["admitted_fraction"] < 1.0:
            return self.fail("traffic_not_fully_admitted", admitted_fraction=truth["admitted_fraction"])
        if customer["failed"] or customer["not_admitted"]:
            return self.fail("customer_requests_still_failing", customer=customer)
        return None

    def evaluate(self):
        app = self.problem.app
        if not self.baseline or self.baseline_truth is None:
            return super().evaluate()
        try:
            if not app.command("get", "deployment", "coordinator", "--ignore-not-found", "-o", "name").strip():
                # Deleting the coordinator removes the symptom by removing the
                # service every customer request depends on.
                return self.fail("coordination_service_missing")

            truth = app.truth()
            customer = app.customer_probe()
            if failed := self.verdict(truth, customer):
                return {**failed, "dropped_requests": truth["dropped_requests"]}

            # Hold it. The cluster re-collapses on its own if the recovery was
            # premature, so a passing instant is not a passing recovery.
            deadline = time.monotonic() + self.durability_seconds
            observed = []
            while time.monotonic() < deadline:
                time.sleep(10)
                truth = app.truth()
                observed.append(
                    {
                        "serve_capacity_fraction": truth["serve_capacity_fraction"],
                        "admitted_fraction": truth["admitted_fraction"],
                        "regressions": truth["regressions"],
                    }
                )
                if regressed := self.verdict(truth, app.customer_probe()):
                    # It passed a moment ago and does not now, so the recovery
                    # was premature rather than merely incomplete. Report that
                    # distinctly, and carry the underlying shortfall as detail.
                    return self.fail(
                        "recovery_not_durable",
                        collapsed_into=regressed["reason"],
                        underlying=regressed.get("detail"),
                        observed=observed,
                        dropped_requests=truth["dropped_requests"],
                    )

            budget = loss_budget(app.recovery_floor_seconds)
            if truth["dropped_requests"] > budget:
                return self.fail(
                    "excessive_requests_dropped",
                    dropped_requests=truth["dropped_requests"],
                    budget=budget,
                    regressions=truth["regressions"],
                )

            result = super().evaluate()
            if not result.get("success"):
                return result
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            **result,
            "serve_capacity_fraction": truth["serve_capacity_fraction"],
            "admitted_fraction": truth["admitted_fraction"],
            "dropped_requests": truth["dropped_requests"],
            "dropped_request_budget": loss_budget(app.recovery_floor_seconds),
            "regressions": truth["regressions"],
            "leader_elections": truth["leader_elections"],
            "members_available": truth["members_available"],
            "recovery_floor_seconds": app.recovery_floor_seconds,
            "held_for_seconds": self.durability_seconds,
        }
