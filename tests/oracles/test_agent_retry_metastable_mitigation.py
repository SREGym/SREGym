"""Tests for AgentRetryMetastableMitigationOracle."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from sregym.conductor.oracles.agent_retry_metastable_mitigation import AgentRetryMetastableMitigationOracle
from sregym.generators.workload.agentic_retry_workload import WorkloadSnapshot


def test_mitigation_oracle_passes_when_healthy():
    problem = SimpleNamespace()
    oracle = AgentRetryMetastableMitigationOracle(
        problem=problem,
        min_success_rate=0.95,
        max_p95_latency=1.0,
        max_queue_depth=5,
        max_amplification=1.5,
    )
    oracle.recovery_timeout_seconds = 2.0
    oracle.poll_interval_seconds = 0.05
    oracle.sample_seconds = 0.05

    # Healthy snapshot
    healthy_snapshot = WorkloadSnapshot(
        submitted=25,
        completed=25,
        succeeded=25,
        failed=0,
        actual_rate=10.0,
        success_rate=1.0,
        p95_latency_seconds=0.25,
        amplification_ratio=1.05,
        backend_queue_depth=0,
        db_pool_waiting=0,
        backend_active_requests=2,
        goodput_rate=10.0,
    )
    workload = MagicMock()
    workload.snapshot.return_value = healthy_snapshot
    problem.workload = workload

    result = oracle.evaluate()
    assert result.get("success") is True


def test_mitigation_oracle_fails_when_traffic_degraded():
    problem = SimpleNamespace()
    oracle = AgentRetryMetastableMitigationOracle(
        problem=problem,
        min_success_rate=0.95,
    )
    oracle.recovery_timeout_seconds = 0.2
    oracle.poll_interval_seconds = 0.05
    oracle.sample_seconds = 0.05

    # Degraded snapshot (success rate low)
    degraded_snapshot = WorkloadSnapshot(
        submitted=25,
        completed=25,
        succeeded=10,
        failed=15,
        actual_rate=10.0,
        success_rate=0.40,
        p95_latency_seconds=0.25,
        amplification_ratio=1.0,
        backend_queue_depth=0,
        db_pool_waiting=0,
        backend_active_requests=2,
        goodput_rate=4.0,
    )
    problem.workload = MagicMock()
    problem.workload.snapshot.return_value = degraded_snapshot

    result = oracle.evaluate()
    assert result.get("success") is False
    assert result.get("reason") in ("traffic_did_not_recover", "insufficient_traffic")


def test_mitigation_oracle_fails_when_traffic_insufficient():
    """Verify that scaling replicas to 0 or stopping traffic fails mitigation."""
    problem = SimpleNamespace()
    oracle = AgentRetryMetastableMitigationOracle(problem=problem)
    oracle.recovery_timeout_seconds = 0.2
    oracle.poll_interval_seconds = 0.05
    oracle.sample_seconds = 0.05

    # Zero-traffic snapshot (e.g. pods scaled to 0)
    zero_snapshot = WorkloadSnapshot(
        submitted=5,
        completed=2,
        succeeded=2,
        failed=0,
        actual_rate=1.0,
        success_rate=1.0,
        p95_latency_seconds=0.1,
        amplification_ratio=1.0,
        backend_queue_depth=0,
        db_pool_waiting=0,
        backend_active_requests=0,
        goodput_rate=1.0,
    )
    problem.workload = MagicMock()
    problem.workload.snapshot.return_value = zero_snapshot

    result = oracle.evaluate()
    assert result.get("success") is False
    assert result.get("reason") == "insufficient_traffic"


def test_mitigation_oracle_fails_when_stability_test_crashes():
    """Verify that a crash during the stability perturbation fails with an error, not success."""
    problem = SimpleNamespace()
    oracle = AgentRetryMetastableMitigationOracle(problem=problem)
    oracle.recovery_timeout_seconds = 0.5
    oracle.poll_interval_seconds = 0.05
    oracle.sample_seconds = 0.05

    healthy_snapshot = WorkloadSnapshot(
        submitted=25,
        completed=25,
        succeeded=25,
        failed=0,
        actual_rate=10.0,
        success_rate=1.0,
        p95_latency_seconds=0.25,
        amplification_ratio=1.05,
        backend_queue_depth=0,
        db_pool_waiting=0,
        backend_active_requests=2,
        goodput_rate=10.0,
    )
    workload = MagicMock()
    workload.snapshot.return_value = healthy_snapshot
    workload.inject_latency_fault.side_effect = RuntimeError("Cluster connection broken")
    problem.workload = workload

    result = oracle.evaluate()
    assert result.get("success") is False
    assert result.get("reason") == "stability_test_exception"
