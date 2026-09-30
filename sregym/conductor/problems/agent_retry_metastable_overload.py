"""Problem implementation for agent_retry_metastable_overload.

Category: Metastable Failure / Application + Distributed Systems
Application: agentic-retry-platform

Unlike conventional RPC retry storms, this scenario models speculative replanning
and uncancelled orphaned work compounded with multi-layer nested retries across
autonomous workflow, tool-gateway, and data transport layers. A transient backend
degradation causes logical requests to expand into multiple correlated backend attempts,
resulting in persistent overload after the initiating disturbance is removed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sregym.conductor.oracles.agent_retry_metastable_diagnosis import AgentRetryMetastableDiagnosisOracle
from sregym.conductor.oracles.agent_retry_metastable_mitigation import AgentRetryMetastableMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.workload.agentic_retry_workload import AgenticRetryWorkload
from sregym.service.apps.agentic_retry_platform import AgenticRetryPlatform
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.conductor.problems.agent_retry_metastable_overload")


class AgentRetryMetastableOverload(Problem):
    """Multi-layer retry amplification in an agentic workflow causing metastable overload."""

    run_default_workload = False

    base_rate = 10.0
    concurrency_limit = 25
    normal_latency = 0.10
    fault_latency = 1.50
    trigger_duration_seconds = 10.0

    baseline_warmup_seconds = 5.0
    post_fault_settle_seconds = 5.0

    def __init__(self):
        super().__init__(app=AgenticRetryPlatform(embedded=False))
        self.faulty_service = ["agent-orchestrator", "tool-gateway", "data-api"]
        self.workload = AgenticRetryWorkload(
            namespace=self.namespace,
            base_rate=self.base_rate,
            concurrency_limit=self.concurrency_limit,
            normal_latency=self.normal_latency,
            fault_latency=self.fault_latency,
            trigger_duration_seconds=self.trigger_duration_seconds,
            planner_max_retries=3,
            tool_max_retries=2,
            transport_max_retries=2,
        )
        self.app.workload = self.workload

        self.root_cause = self.build_structured_root_cause(
            component="agent-workflow/retry-policy",
            namespace=self.namespace,
            description=(
                "A transient backend slowdown caused the agent planner deadline to expire, launching "
                "speculative replacement tool operations across generations without cancelling earlier in-flight "
                "operations. Uncancelled orphaned work combined with nested tool and transport retries "
                "saturated backend concurrency and connection pool capacity. Physical work remained trapped in a "
                "self-sustaining metastable overload loop long after the initiating latency perturbation was removed. "
                "The sustaining cause is the uncoordinated end-to-end retry policy lacking child cancellation, unified "
                "retry budgets, and proper timeout hierarchy, not the expired backend slowdown."
            ),
        )

        self.diagnosis_oracle = AgentRetryMetastableDiagnosisOracle(
            problem=self,
            expected=self.root_cause,
        )
        self.mitigation_oracle = AgentRetryMetastableMitigationOracle(problem=self)
        self._injection_attempted = False

    def _verify_healthy_baseline(self):
        print(f"[Baseline] Warming up agent workflow at {self.base_rate:.0f} req/s...")
        time.sleep(self.baseline_warmup_seconds)
        snapshot = self.workload.snapshot(window_seconds=self.baseline_warmup_seconds)
        print(
            f"[Baseline] rate={snapshot.actual_rate:.1f} req/s success={snapshot.success_rate:.1%} "
            f"amp={snapshot.amplification_ratio:.2f} queue={snapshot.backend_queue_depth}"
        )

        if snapshot.success_rate < 0.90:
            raise RuntimeError(f"Baseline success rate too low: {snapshot.success_rate:.1%}")
        if snapshot.amplification_ratio > 1.3:
            raise RuntimeError(f"Baseline amplification unexpectedly high: {snapshot.amplification_ratio:.2f}")

    def _inject_and_verify_metastable_loop(self):
        print(
            f"[Trigger] Injecting temporary backend latency ({self.fault_latency}s) for {self.trigger_duration_seconds}s..."
        )
        self.workload.inject_latency_fault()
        try:
            time.sleep(self.trigger_duration_seconds)
        finally:
            print("[Trigger] Ending temporary backend latency perturbation; backend returned to normal service time")
            self.workload.remove_latency_fault()

        # Allow time to observe whether the system self-recovers or stays degraded
        print(f"[Observation] Observing post-fault behavior for {self.post_fault_settle_seconds}s...")
        time.sleep(self.post_fault_settle_seconds)
        snapshot = self.workload.snapshot(window_seconds=self.post_fault_settle_seconds)

        print(
            f"[Post-fault] rate={snapshot.actual_rate:.1f} req/s success={snapshot.success_rate:.1%} "
            f"amp={snapshot.amplification_ratio:.2f} queue={snapshot.backend_queue_depth} "
            f"active_workers={snapshot.backend_active_requests}"
        )

        # Metastable verification: fault is gone, but system must remain degraded
        failures = []
        if snapshot.amplification_ratio < 1.8:
            failures.append(
                f"retries did not sufficiently amplify backend load (amp={snapshot.amplification_ratio:.2f})"
            )
        if snapshot.backend_queue_depth < 10 and snapshot.backend_active_requests < self.concurrency_limit:
            failures.append(f"backend queue and workers are not saturated (queue={snapshot.backend_queue_depth})")
        if snapshot.success_rate > 0.60:
            failures.append(
                f"system recovered spontaneously instead of remaining in metastable state (success={snapshot.success_rate:.1%})"
            )

        if failures:
            raise RuntimeError("Metastable overload state failed to establish: " + "; ".join(failures))

        print("[Metastable State Confirmed] Service remains severely degraded despite fault removal.")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: agent_retry_metastable_overload ==")
        if self.fault_injected or self._injection_attempted:
            raise RuntimeError("Fault injection already active or attempted")
        self._injection_attempted = True

        self.workload.start()
        try:
            self._verify_healthy_baseline()
            self._inject_and_verify_metastable_loop()
        except Exception:
            self.recover_fault()
            raise

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: agent_retry_metastable_overload ==")
        self.workload.remove_latency_fault()
        self.workload.apply_mitigation(
            cap_planner_retries=1,
            disable_nested_retries=True,
            enable_backoff=True,
            shed_stale_queue=True,
        )
        # Give time for queue to drain
        time.sleep(1.0)

    def stop_workload(self):
        self.workload.stop()

    def run_negative_control(self) -> dict[str, Any]:
        """Execute negative control: identical perturbation with capped retries (R_planner=1, unified budget).

        Proves that without stacked planner replanning, the system quickly self-recovers after the
        perturbation ends, demonstrating that the uncoordinated retry policy is causal.
        """
        print("== Negative Control: Single Unified Retry Layer ==")
        control_workload = AgenticRetryWorkload(
            namespace=self.namespace,
            base_rate=self.base_rate,
            concurrency_limit=self.concurrency_limit,
            normal_latency=self.normal_latency,
            fault_latency=self.fault_latency,
            planner_max_retries=1,  # R_planner = 0 replans
            tool_max_retries=1,
            transport_max_retries=2,
        )
        control_workload.start()
        try:
            time.sleep(self.baseline_warmup_seconds)
            baseline = control_workload.snapshot(self.baseline_warmup_seconds)

            control_workload.inject_latency_fault()
            time.sleep(self.trigger_duration_seconds)
            control_workload.remove_latency_fault()

            time.sleep(self.post_fault_settle_seconds)
            post_trigger = control_workload.snapshot(self.post_fault_settle_seconds)

            recovered = (
                post_trigger.success_rate >= 0.90
                and post_trigger.amplification_ratio <= 1.5
                and post_trigger.backend_queue_depth <= 5
            )
            print(
                f"[Negative Control Result] recovered={recovered} "
                f"post_success={post_trigger.success_rate:.1%} "
                f"post_amp={post_trigger.amplification_ratio:.2f} "
                f"post_queue={post_trigger.backend_queue_depth}"
            )
            return {
                "recovered": recovered,
                "baseline": baseline,
                "post_trigger": post_trigger,
            }
        finally:
            control_workload.stop()


if __name__ == "__main__":
    problem = AgentRetryMetastableOverload()
    problem.inject_fault()
    time.sleep(2)
    problem.recover_fault()
