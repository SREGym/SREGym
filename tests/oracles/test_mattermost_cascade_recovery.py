"""The grading contract for a misleading signal and harmful capacity automation.

The lesson of this family is that a manual scale-up is not a fix while the
automation that shrank the service is still running. These tests pin that down.
"""

import pytest

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mattermost_cascade_recovery import MattermostCascadeOracle, latency_budget
from sregym.service.apps.incident_runtime.capacity_scaler import DEFAULT_POLICY, decide, observe


def test_cpu_policy_removes_capacity_from_a_saturated_gateway():
    """The incident in one line: blocked workers look like spare capacity."""
    rules = {**DEFAULT_POLICY, "scale_in_below": 25, "min": 1}

    target, why = decide(3, 2.0, rules, "cpu")

    assert target == 2
    assert "below" in why


def test_cpu_policy_will_not_stop_above_the_service_floor():
    """Left alone it walks all the way down to its own floor, not the service's."""
    rules = {**DEFAULT_POLICY, "scale_in_below": 25, "min": 1}
    replicas = 3
    for _ in range(5):
        replicas, _ = decide(replicas, 2.0, rules, "cpu")

    assert replicas == 1


def test_raising_the_policy_floor_is_a_valid_repair():
    rules = {**DEFAULT_POLICY, "scale_in_below": 25, "min": 3}

    assert decide(3, 2.0, rules, "cpu")[0] == 3


def test_keying_on_saturation_reads_the_incident_the_right_way_round():
    rules = {**DEFAULT_POLICY, "scale_out_above": 60, "max": 6}

    target, why = decide(3, 100.0, rules, "saturation")

    assert target == 4
    assert "saturation" in why


def test_a_healthy_gateway_is_left_alone_under_either_metric():
    rules = {**DEFAULT_POLICY, "scale_in_below": 25, "scale_out_above": 60}

    assert decide(3, 40.0, rules, "cpu")[0] == 3
    assert decide(3, 40.0, rules, "saturation")[0] == 3


def test_a_missing_metric_never_moves_capacity():
    """A scraping failure must not be read as an idle service."""
    target, why = decide(3, None, DEFAULT_POLICY, "cpu")

    assert target == 3
    assert why == "no metric available"


def test_observe_averages_only_pods_that_reported():
    samples = [{"cpu_percent": 10.0}, {"saturation_percent": 100.0}, {"cpu_percent": 20.0}]

    assert observe(samples, "cpu_percent") == 15.0
    assert observe([], "cpu_percent") is None


def test_latency_budget_is_calibrated_but_never_absurdly_tight():
    # A fast host must not get a budget so small that healthy jitter fails it.
    assert latency_budget(1.0) == 500.0
    assert latency_budget(200.0) == 1200.0


@pytest.mark.parametrize(
    "reason",
    [
        "gateway_capacity_below_floor",
        "capacity_automation_still_shrinking",
        "gateway_shedding_requests",
        "gateway_latency_unresolved",
        "gateway_missing",
    ],
)
def test_every_shortfall_is_a_classified_agent_error(reason):
    """`reason` reaches the results CSV; an unclassified one files as ambiguous."""
    assert MattermostCascadeOracle._failure_classes()[reason] == FailureClass.AGENT_ERROR


def test_the_shared_saas_checks_are_still_reachable():
    classes = MattermostCascadeOracle._failure_classes()

    assert classes["acknowledged_record_missing"] == FailureClass.AGENT_ERROR
    assert classes["database_membership_changed"] == FailureClass.AGENT_ERROR


def test_capacity_is_observed_across_more_than_one_automation_interval():
    """Sampling once would pass a scale-up the policy is about to undo."""
    from types import SimpleNamespace

    problem = SimpleNamespace(app=SimpleNamespace(scaler_interval=15))
    oracle = MattermostCascadeOracle.__new__(MattermostCascadeOracle)
    oracle.problem = problem

    assert oracle.stability_seconds > 2 * 15


def test_capacity_automation_does_nothing_without_a_calibrated_policy():
    """Startup must not resize the service against an uncalibrated threshold."""
    assert DEFAULT_POLICY["enabled"] is False
