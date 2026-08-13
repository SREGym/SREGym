from types import SimpleNamespace
from unittest.mock import Mock

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
    oracle._cluster_shape_healthy = Mock(return_value=True)
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

    assert oracle._healthy_sample() is True


def test_healthy_sample_rejects_a_backlogged_retry_loop(monkeypatch):
    before = {"search_requests_total": 100, "search_rate_attempts_total": 300}
    after = {
        "search_requests_total": 180,
        "search_rate_attempts_total": 540,
        "rate_queue_depth": 256,
    }
    observed = WorkloadSnapshot(80, 80, 0, 8.0, 0.0, 3.0)
    oracle = _oracle([before, after], observed)
    monkeypatch.setattr("sregym.conductor.oracles.search_rate_retry_mitigation.time.sleep", lambda _: None)

    assert oracle._healthy_sample() is False


def test_runtime_policy_accepts_the_standard_operating_envelope():
    oracle = _oracle(
        [{"rate_backend_qps_limit": 500, "rate_queue_capacity": 256}],
        WorkloadSnapshot(0, 0, 0, 0.0, 0.0, None),
    )

    assert oracle._runtime_policy_within_safe_bounds() is True


def test_runtime_policy_rejects_an_effectively_disabled_qps_limit():
    oracle = _oracle(
        [{"rate_backend_qps_limit": 100000, "rate_queue_capacity": 256}],
        WorkloadSnapshot(0, 0, 0, 0.0, 0.0, None),
    )

    assert oracle._runtime_policy_within_safe_bounds() is False


def test_runtime_policy_rejects_an_oversized_backlog():
    oracle = _oracle(
        [{"rate_backend_qps_limit": 50, "rate_queue_capacity": 8192}],
        WorkloadSnapshot(0, 0, 0, 0.0, 0.0, None),
    )

    assert oracle._runtime_policy_within_safe_bounds() is False


def test_evaluate_requires_recovery_before_and_after_trigger_replay():
    workload = SimpleNamespace(start=Mock(), set_rate=Mock(), stop=Mock())
    problem = SimpleNamespace(base_rate=8.0, workload=workload)
    oracle = SearchRateRetryMitigationOracle(problem)
    oracle._baseline_deployments = {"frontend", "search", "rate"}
    oracle._runtime_policy_within_safe_bounds = Mock(return_value=True)
    oracle._wait_for_healthy_state = Mock(side_effect=[True, True])
    oracle._replay_trigger = Mock(return_value=True)
    oracle._cluster_shape_healthy = Mock(return_value=True)

    assert oracle.evaluate()["success"] is True
    assert oracle._wait_for_healthy_state.call_count == 2
    oracle._replay_trigger.assert_called_once_with()
    assert oracle._runtime_policy_within_safe_bounds.call_count == 2
    workload.stop.assert_called_once_with()
