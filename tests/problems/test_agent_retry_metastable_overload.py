"""Tests for agent_retry_metastable_overload problem, architecture, and dynamics."""

import time

from sregym.conductor.problems.agent_retry_metastable_overload import AgentRetryMetastableOverload
from sregym.generators.workload.agentic_retry_workload import AgenticRetryWorkload


def test_problem_metadata_and_attributes():
    problem = AgentRetryMetastableOverload()
    assert problem.namespace == "agentic-retry-platform"
    assert problem.run_default_workload is False
    assert problem.base_rate == 10.0
    assert problem.concurrency_limit == 25
    assert problem.fault_latency == 1.50
    assert "agent-orchestrator" in problem.faulty_service
    assert "tool-gateway" in problem.faulty_service
    assert "data-api" in problem.faulty_service
    assert "retry-policy" in problem.root_cause


def test_workload_healthy_baseline_metrics():
    """Verify that in healthy baseline state, amplification A(t) ~ 1.0 and queue depth is 0."""
    workload = AgenticRetryWorkload(
        base_rate=15.0,
        concurrency_limit=25,
        normal_latency=0.02,
        fault_latency=0.2,
        test_mode=True,
    )
    workload.start()
    try:
        time.sleep(0.1)
        snapshot = workload.snapshot(window_seconds=0.1)
        assert snapshot.success_rate >= 0.90
        assert snapshot.amplification_ratio <= 1.3
        assert snapshot.backend_queue_depth == 0
        assert snapshot.db_pool_waiting == 0
    finally:
        workload.stop()


def test_workload_metastable_loop_and_mitigation():
    """Verify that transient fault triggers amplification and mitigation collapses the storm."""
    workload = AgenticRetryWorkload(
        base_rate=20.0,
        concurrency_limit=6,
        normal_latency=0.08,
        fault_latency=0.35,
        test_mode=True,
    )
    workload.start()
    try:
        # 1. Healthy baseline
        snapshot = workload.snapshot(0.1)
        assert snapshot.success_rate >= 0.90
        assert snapshot.amplification_ratio <= 1.3

        # 2. Inject fault
        workload.inject_latency_fault(latency_ms=350.0, duration_seconds=0.2)
        snapshot = workload.snapshot(0.1)
        assert snapshot.amplification_ratio > 2.0
        assert snapshot.backend_queue_depth > 0

        # 3. Remove fault - system remains degraded (metastable state)
        workload.remove_latency_fault()
        snapshot = workload.snapshot(0.1)
        assert snapshot.amplification_ratio > 1.5
        assert snapshot.success_rate < 0.65

        # 4. Apply mitigation - collapses metastable storm
        workload.apply_mitigation(
            cap_planner_retries=1,
            disable_nested_retries=True,
            enable_cancellation=True,
            enable_retry_budget=True,
        )
        snapshot = workload.snapshot(0.1)
        assert snapshot.success_rate >= 0.90
        assert snapshot.amplification_ratio <= 1.3
        assert snapshot.backend_queue_depth == 0
    finally:
        workload.stop()
