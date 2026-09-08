from types import SimpleNamespace
from unittest.mock import Mock

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.search_rate_retry_mitigation import (
    SearchRateRetryMitigationOracle,
)
from sregym.generators.workload.hotel_search import WorkloadSnapshot


def _deployment(*, replicas=1, generation=2, observed=2, ready=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(generation=generation),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(
            observed_generation=observed,
            replicas=replicas,
            updated_replicas=replicas,
            ready_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=0,
        ),
    )


def _oracle(metric_snapshots, workload_snapshot):
    metrics = SimpleNamespace(snapshot=Mock(side_effect=metric_snapshots))
    workload = SimpleNamespace(metrics=metrics, snapshot=Mock(return_value=workload_snapshot))
    problem = SimpleNamespace(
        base_rate=8.0,
        workload=workload,
        maximum_safe_backend_qps_limit=500,
        maximum_safe_queue_capacity=256,
    )
    oracle = SearchRateRetryMitigationOracle(problem)
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    return oracle


def test_rollout_requires_current_ready_nonzero_replicas():
    assert SearchRateRetryMitigationOracle._rollout_complete(_deployment()) is True
    assert SearchRateRetryMitigationOracle._rollout_complete(_deployment(replicas=0, ready=0)) is False
    assert SearchRateRetryMitigationOracle._rollout_complete(_deployment(observed=1)) is False
    assert SearchRateRetryMitigationOracle._rollout_complete(_deployment(ready=0)) is False


def test_healthy_sample_accepts_functional_unamplified_traffic(monkeypatch):
    before = {"search_requests_total": 100, "search_rate_attempts_total": 100}
    after = {
        "search_requests_total": 180,
        "search_rate_attempts_total": 181,
        "rate_queue_depth": 0,
    }
    observed = WorkloadSnapshot(80, 80, 80, 8.0, 1.0, 0.2)
    oracle = _oracle([before, after], observed)
    monkeypatch.setattr("sregym.conductor.oracles.search_rate_retry_mitigation.time.sleep", lambda _: None)

    assert oracle._unhealthy_sample() is None


def test_a_backlogged_retry_loop_is_behavioural_not_environmental(monkeypatch):
    """Cluster shape checked out, so this is about the mitigation.

    The sample now reports which of the two it was. Previously both a broken
    cluster and an unrecovered application surfaced as "traffic did not
    recover", so an environmental failure was recorded as a behavioural one.
    """
    before = {"search_requests_total": 100, "search_rate_attempts_total": 300}
    after = {
        "search_requests_total": 180,
        "search_rate_attempts_total": 540,
        "rate_queue_depth": 256,
    }
    observed = WorkloadSnapshot(80, 80, 0, 8.0, 0.0, 3.0)
    oracle = _oracle([before, after], observed)
    monkeypatch.setattr("sregym.conductor.oracles.search_rate_retry_mitigation.time.sleep", lambda _: None)

    verdict = oracle._unhealthy_sample()

    assert verdict["reason"] == "traffic_did_not_recover"
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR
    assert verdict["detail"]["attempts_per_request"] == 3.0


def test_an_unsettled_rollout_keeps_its_own_reason(monkeypatch):
    """The regression the verdict-returning sample exists to prevent."""
    oracle = _oracle([{}], WorkloadSnapshot(0, 0, 0, 0.0, 0.0, None))
    oracle._cluster_shape_unhealthy = Mock(
        return_value=SearchRateRetryMitigationOracle.fail("required_deployment_not_rolled_out")
    )

    verdict = oracle._unhealthy_sample()

    assert verdict["reason"] == "required_deployment_not_rolled_out"
    assert verdict["failure_class"] == FailureClass.AMBIGUOUS


def test_runtime_policy_accepts_the_standard_operating_envelope():
    oracle = _oracle(
        [{"rate_backend_qps_limit": 500, "rate_queue_capacity": 256}],
        WorkloadSnapshot(0, 0, 0, 0.0, 0.0, None),
    )

    assert oracle._runtime_policy_outside_safe_bounds() is None


def test_an_effectively_disabled_qps_limit_is_the_agents_doing():
    """Recovery bought by removing backpressure rather than fixing retries."""
    oracle = _oracle(
        [{"rate_backend_qps_limit": 100000, "rate_queue_capacity": 256}],
        WorkloadSnapshot(0, 0, 0, 0.0, 0.0, None),
    )

    verdict = oracle._runtime_policy_outside_safe_bounds()

    assert verdict["reason"] == "qps_limit_outside_safe_envelope"
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR
    assert verdict["detail"] == {"qps_limit": 100000, "maximum": 500}


def test_an_oversized_backlog_is_the_agents_doing():
    oracle = _oracle(
        [{"rate_backend_qps_limit": 50, "rate_queue_capacity": 8192}],
        WorkloadSnapshot(0, 0, 0, 0.0, 0.0, None),
    )

    verdict = oracle._runtime_policy_outside_safe_bounds()

    assert verdict["reason"] == "queue_capacity_outside_safe_envelope"
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR


def test_a_missing_deployment_is_attributed_here_because_a_baseline_exists():
    """The per-oracle override, exercised.

    Everywhere else a missing Deployment is ambiguous. This oracle snapshots
    the Deployment names while the app is healthy, so it has the *before* that
    makes attribution possible.
    """
    verdict = SearchRateRetryMitigationOracle.fail("required_deployment_missing")
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR


def test_evaluate_requires_recovery_before_and_after_trigger_replay():
    workload = SimpleNamespace(start=Mock(), set_rate=Mock(), stop=Mock())
    problem = SimpleNamespace(base_rate=8.0, workload=workload)
    oracle = SearchRateRetryMitigationOracle(problem)
    oracle._baseline_deployments = {"frontend", "search", "rate"}
    oracle._runtime_policy_outside_safe_bounds = Mock(return_value=None)
    oracle._wait_for_healthy_state = Mock(side_effect=[None, None])
    oracle._replay_trigger = Mock(return_value=True)
    oracle._cluster_shape_unhealthy = Mock(return_value=None)

    assert oracle.evaluate()["success"] is True
    assert oracle._wait_for_healthy_state.call_count == 2
    oracle._replay_trigger.assert_called_once_with()
    assert oracle._runtime_policy_outside_safe_bounds.call_count == 2
    workload.stop.assert_called_once_with()
