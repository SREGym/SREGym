"""Behavioral mitigation oracle for search retry amplification."""

from __future__ import annotations

import time

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class SearchRateRetryMitigationOracle(Oracle):
    """Require recovery now and after replaying the temporary load trigger."""

    importance = 1.0
    poll_interval_seconds = 5
    sample_seconds = 10
    initial_recovery_timeout_seconds = 150
    replay_recovery_timeout_seconds = 120
    required_services = ("frontend", "search", "rate")

    def __init__(self, problem):
        super().__init__(problem)
        self._baseline_deployments: set[str] = set()

    def capture_baseline(self) -> None:
        deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(namespace=self.problem.namespace)
        self._baseline_deployments = {deployment.metadata.name for deployment in deployments.items}

    @staticmethod
    def _rollout_complete(deployment) -> bool:
        desired = deployment.spec.replicas if deployment.spec.replicas is not None else 1
        status = deployment.status
        return desired >= 1 and (
            (status.observed_generation or 0) >= (deployment.metadata.generation or 0)
            and (status.replicas or 0) == desired
            and (status.updated_replicas or 0) == desired
            and (status.ready_replicas or 0) == desired
            and (status.available_replicas or 0) == desired
            and (status.unavailable_replicas or 0) == 0
        )

    def _cluster_shape_healthy(self) -> bool:
        try:
            deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(namespace=self.problem.namespace)
            current = {deployment.metadata.name: deployment for deployment in deployments.items}
            missing = sorted(self._baseline_deployments - current.keys())
            if missing:
                print(f"[FAIL] Required Deployments are missing: {', '.join(missing)}")
                return False
            if any(not self._rollout_complete(current[name]) for name in self._baseline_deployments):
                print("[FAIL] One or more application Deployments are not fully rolled out and Ready")
                return False

            for service_name in self.required_services:
                endpoints = self.problem.kubectl.core_v1_api.read_namespaced_endpoints(
                    name=service_name,
                    namespace=self.problem.namespace,
                )
                if not any(subset.addresses for subset in endpoints.subsets or []):
                    print(f"[FAIL] Service {service_name!r} has no Ready endpoints")
                    return False
        except ApiException as exc:
            print(f"[FAIL] Could not verify the application topology: {exc}")
            return False
        return True

    @staticmethod
    def _delta(before: dict[str, float], after: dict[str, float], name: str) -> float:
        return after.get(name, 0.0) - before.get(name, 0.0)

    def _healthy_sample(self) -> bool:
        if not self._cluster_shape_healthy():
            return False

        try:
            before = self.problem.workload.metrics.snapshot()
            time.sleep(self.sample_seconds)
            after = self.problem.workload.metrics.snapshot()
        except Exception as exc:
            print(f"[FAIL] Application metrics could not be read: {exc}")
            return False

        observed = self.problem.workload.snapshot(self.sample_seconds)
        search_requests = self._delta(before, after, "search_requests_total")
        rate_attempts = self._delta(before, after, "search_rate_attempts_total")
        amplification = rate_attempts / search_requests if search_requests > 0 else float("inf")
        queue_depth = after.get("rate_queue_depth", -1)
        minimum_completed = self.problem.base_rate * self.sample_seconds * 0.75
        healthy = (
            self.problem.base_rate * 0.80 <= observed.actual_rate <= self.problem.base_rate * 1.20
            and observed.completed >= minimum_completed
            and observed.success_rate >= 0.90
            and search_requests >= minimum_completed
            and amplification <= 2.10
            and 0 <= queue_depth <= 5
        )
        print(
            "[Health] "
            f"rate={observed.actual_rate:.2f}/s completed={observed.completed} "
            f"success={observed.success_rate:.1%} attempts/request={amplification:.2f} "
            f"queue={queue_depth:.0f}"
        )
        return healthy

    def _wait_for_healthy_state(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._healthy_sample():
                return True
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.poll_interval_seconds, remaining))
        return False

    def _runtime_policy_within_safe_bounds(self) -> bool:
        """Reject recovery obtained by effectively disabling backpressure."""
        try:
            metrics = self.problem.workload.metrics.snapshot()
        except Exception as exc:
            print(f"[FAIL] The effective rate policy could not be read: {exc}")
            return False

        qps_limit = metrics.get("rate_backend_qps_limit")
        queue_capacity = metrics.get("rate_queue_capacity")
        if qps_limit is None or queue_capacity is None:
            print("[FAIL] The rate service did not expose its effective admission-control policy")
            return False

        maximum_qps = self.problem.maximum_safe_backend_qps_limit
        maximum_queue = self.problem.maximum_safe_queue_capacity
        if not 0 < qps_limit <= maximum_qps:
            print(
                "[FAIL] The effective backend QPS limit is outside the safe operating envelope: "
                f"{qps_limit:g} (maximum {maximum_qps:g})"
            )
            return False
        if not 0 < queue_capacity <= maximum_queue:
            print(
                "[FAIL] The effective queue capacity is outside the safe operating envelope: "
                f"{queue_capacity:g} (maximum {maximum_queue:g})"
            )
            return False

        print(f"[Policy] backend_qps_limit={qps_limit:g} queue_capacity={queue_capacity:g}")
        return True

    def _replay_trigger(self) -> bool:
        self.problem.workload.set_rate(self.problem.trigger_rate)
        try:
            time.sleep(self.problem.trigger_seconds)
            observed = self.problem.workload.snapshot(self.problem.trigger_seconds)
        finally:
            self.problem.workload.set_rate(self.problem.base_rate)
        print(
            f"[Replay] Sent {observed.actual_rate:.2f} requests/s for "
            f"{self.problem.trigger_seconds:.0f}s, then restored the normal rate"
        )
        return observed.actual_rate >= self.problem.trigger_rate * 0.80

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Search Retry Mitigation Evaluation ==")
        if not self._baseline_deployments:
            print("[FAIL] No healthy baseline was captured")
            return {"success": False}

        try:
            self.problem.workload.start()
            self.problem.workload.set_rate(self.problem.base_rate)

            if not self._runtime_policy_within_safe_bounds():
                return {"success": False}

            if not self._wait_for_healthy_state(self.initial_recovery_timeout_seconds):
                print("[FAIL] Normal search traffic did not recover")
                return {"success": False}

            if not self._replay_trigger():
                print("[FAIL] The protected trigger workload was not delivered at the required rate")
                return {"success": False}

            if not self._wait_for_healthy_state(self.replay_recovery_timeout_seconds):
                print("[FAIL] The application did not recover after the temporary trigger ended")
                return {"success": False}

            if not self._runtime_policy_within_safe_bounds():
                return {"success": False}

            if not self._cluster_shape_healthy():
                return {"success": False}
            self.problem.workload.stop()
        except Exception as exc:
            print(f"[FAIL] Error while verifying mitigation: {exc}")
            return {"success": False}

        print("[PASS] Normal traffic is healthy and recovers after replaying the temporary trigger")
        return {"success": True}
