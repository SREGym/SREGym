"""Grading for the long-horizon family: outcome, accumulated cost, and damage.

The earlier families graded only the end state. These tests pin the two
dimensions that are new — cost that accrued on the way, and irreversible damage
left behind — plus the durability hold that stops a mid-flight state passing.
"""

import pytest

from sregym.conductor.oracles.coordination_recovery import CoordinationRecoveryOracle, loss_budget
from sregym.conductor.oracles.failure import FailureClass

RECOVERED = {
    "quorum_lost": False,
    "destroyed_members": [],
    "leader_healthy": True,
    "serve_capacity_fraction": 1.0,
    "admitted_fraction": 1.0,
    "write_latency_ms": 120.0,
    "latency_budget_ms": 400.0,
    "watch_subscriptions": 48,
    "compacted": True,
    "scheduler_state_fresh": True,
    "cache_warm_fraction": 1.0,
    "dropped_requests": 0,
    "regressions": 0,
    "leader_elections": 1,
    "members_available": 3,
}
CLEAN = {"served": 20, "not_admitted": 0, "failed": 0}


def verdict(**overrides):
    """Grade a truth dict without a cluster. `verdict` only reads its arguments."""
    return CoordinationRecoveryOracle.verdict(CoordinationRecoveryOracle, {**RECOVERED, **overrides}, CLEAN)


def test_a_complete_recovery_produces_no_verdict():
    assert verdict() is None


def test_the_untouched_collapse_is_reported_as_an_unstable_leader():
    """The state the agent is handed: latency over budget, so no term holds."""
    outcome = verdict(leader_healthy=False, write_latency_ms=534.0, watch_subscriptions=96)

    assert outcome["reason"] == "coordination_leader_unstable"
    assert outcome["detail"]["write_latency_ms"] == 534.0


def test_shedding_alone_is_not_recovery():
    """Leader holds, but placement data is stale so almost nothing serves."""
    outcome = verdict(serve_capacity_fraction=0.1, compacted=False, scheduler_state_fresh=False)

    assert outcome["reason"] == "coordination_not_serving"
    assert outcome["detail"]["scheduler_state_fresh"] is False


def test_a_repaired_cluster_with_traffic_still_shut_off_is_not_recovery():
    """Capacity restored but customers still not served is not a fix."""
    outcome = verdict(admitted_fraction=0.25)

    assert outcome["reason"] == "traffic_not_fully_admitted"


def test_destroying_a_member_fails_even_when_service_is_back():
    """Full capacity on a cluster that can no longer lose a member is not a fix."""
    outcome = verdict(destroyed_members=["coordinator-1"], members_available=2)

    assert outcome["reason"] == "cluster_redundancy_destroyed"
    assert outcome["detail"]["destroyed"] == ["coordinator-1"]


def test_losing_quorum_is_reported_ahead_of_everything_else():
    """The one-way door: no later action can restore service, so say that."""
    outcome = verdict(
        quorum_lost=True,
        destroyed_members=["coordinator-1", "coordinator-2"],
        leader_healthy=False,
        serve_capacity_fraction=0.0,
    )

    assert outcome["reason"] == "quorum_permanently_lost"


def test_customer_failures_are_graded_even_when_the_cluster_looks_right():
    truth = dict(RECOVERED)
    outcome = CoordinationRecoveryOracle.verdict(
        CoordinationRecoveryOracle, truth, {"served": 12, "not_admitted": 3, "failed": 5}
    )

    assert outcome["reason"] == "customer_requests_still_failing"


@pytest.mark.parametrize(
    "reason",
    [
        "coordination_not_serving",
        "coordination_leader_unstable",
        "traffic_not_fully_admitted",
        "customer_requests_still_failing",
        "recovery_not_durable",
        "excessive_requests_dropped",
        "cluster_redundancy_destroyed",
        "quorum_permanently_lost",
        "coordination_service_missing",
    ],
)
def test_every_shortfall_is_a_classified_agent_error(reason):
    """`reason` reaches the results CSV; an unclassified one files as ambiguous."""
    assert CoordinationRecoveryOracle._failure_classes()[reason] == FailureClass.AGENT_ERROR


def test_the_shared_saas_guarantees_are_still_in_force():
    """An agent must not "recover" by discarding business state."""
    classes = CoordinationRecoveryOracle._failure_classes()

    assert classes["acknowledged_record_missing"] == FailureClass.AGENT_ERROR
    assert classes["database_membership_changed"] == FailureClass.AGENT_ERROR


def test_the_loss_budget_scales_with_the_incident_and_leaves_real_headroom():
    """A flawless run loses nothing, so the budget is headroom, not a licence."""
    assert loss_budget(360) == 7200
    assert loss_budget(630) > loss_budget(360)
    # One rushed full admission costs a few hundred requests, so a single
    # recoverable mistake must stay inside the budget.
    assert loss_budget(360) > 400


def test_the_durability_hold_outlasts_the_longest_settling_time():
    """A recovery caught mid-warming, about to re-collapse, must not pass."""
    from types import SimpleNamespace

    oracle = CoordinationRecoveryOracle.__new__(CoordinationRecoveryOracle)
    oracle.problem = SimpleNamespace(app=SimpleNamespace(settings=(90, 180, 60)))

    assert oracle.durability_seconds > 90
    assert oracle.durability_seconds > 60
