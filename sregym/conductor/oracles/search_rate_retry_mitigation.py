"""Behavioral mitigation oracle for search retry amplification."""

from __future__ import annotations

import time

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.rollout import deployment_rollout_complete


class SearchRateRetryMitigationOracle(Oracle):
    """Require recovery now and after replaying the temporary load trigger."""

    importance = 1.0
    poll_interval_seconds = 5
    sample_seconds = 10
    initial_recovery_timeout_seconds = 150
    replay_recovery_timeout_seconds = 120
    required_services = ("frontend", "search", "rate")

    FAILURE_CLASSES = {
        # This oracle is one of the few that records a *before*: capture_baseline
        # snapshots the Deployment names while the app is healthy. So unlike
        # everywhere else, a missing Deployment here is not ambiguous -- it was
        # present, and now it is not, and the fault injection does not delete
        # Deployments. Overriding the shared AMBIGUOUS is exactly what the
        # per-oracle table is for.
        "required_deployment_missing": FailureClass.AGENT_ERROR,
        # Recovery obtained by disabling backpressure rather than fixing the
        # retry amplification: the documented way to game this problem.
        "qps_limit_outside_safe_envelope": FailureClass.AGENT_ERROR,
        "queue_capacity_outside_safe_envelope": FailureClass.AGENT_ERROR,
        # Behavioural failures measured after cluster shape was verified
        # healthy, so they are about the mitigation rather than the cluster.
        "traffic_did_not_recover": FailureClass.AGENT_ERROR,
        "did_not_recover_after_trigger": FailureClass.AGENT_ERROR,
        # We could not read what we needed to judge.
        "metrics_unreadable": FailureClass.ENVIRONMENT_ERROR,
        "policy_not_exposed": FailureClass.ENVIRONMENT_ERROR,
        # capture_baseline never ran or found nothing -- our sequencing problem.
        "baseline_not_captured": FailureClass.HARNESS_ERROR,
        # The load generator did not deliver the replay. Could be the generator
        # or an agent that rate-limited ingress; not separable from here.
        "trigger_load_not_delivered": FailureClass.AMBIGUOUS,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self._baseline_deployments: set[str] = set()

    def capture_baseline(self) -> None:
        deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(namespace=self.problem.namespace)
        self._baseline_deployments = {deployment.metadata.name for deployment in deployments.items}

    @staticmethod
    def _rollout_complete(deployment) -> bool:
        return deployment_rollout_complete(deployment)

    def _cluster_shape_unhealthy(self) -> dict | None:
        try:
            deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(namespace=self.problem.namespace)
            current = {deployment.metadata.name: deployment for deployment in deployments.items}
            missing = sorted(self._baseline_deployments - current.keys())
            if missing:
                print(f"[FAIL] Required Deployments are missing: {', '.join(missing)}")
                return self.fail("required_deployment_missing", deployments=missing)
            unrolled = sorted(name for name in self._baseline_deployments if not self._rollout_complete(current[name]))
            if unrolled:
                print("[FAIL] One or more application Deployments are not fully rolled out and Ready")
                return self.fail("required_deployment_not_rolled_out", deployments=unrolled)

            for service_name in self.required_services:
                endpoints = self.problem.kubectl.core_v1_api.read_namespaced_endpoints(
                    name=service_name,
                    namespace=self.problem.namespace,
                )
                if not any(subset.addresses for subset in endpoints.subsets or []):
                    print(f"[FAIL] Service {service_name!r} has no Ready endpoints")
                    return self.fail("no_ready_endpoints", service=service_name)
        except ApiException as exc:
            print(f"[FAIL] Could not verify the application topology: {exc}")
            return self.fail_from_exception(exc)
        return None

    @staticmethod
    def _delta(before: dict[str, float], after: dict[str, float], name: str) -> float:
        return after.get(name, 0.0) - before.get(name, 0.0)

    def _unhealthy_sample(self) -> dict | None:
        """Return a verdict describing why the sample was unhealthy, else None.

        Returning the verdict rather than a bool matters more here than
        elsewhere: a broken cluster and an unrecovered application both used to
        surface as "traffic did not recover", so an environmental failure was
        reported as a behavioural one. The caller now propagates whichever
        verdict the last sample produced.
        """
        unhealthy = self._cluster_shape_unhealthy()
        if unhealthy is not None:
            return unhealthy

        try:
            before = self.problem.workload.metrics.snapshot()
            time.sleep(self.sample_seconds)
            after = self.problem.workload.metrics.snapshot()
        except Exception as exc:
            print(f"[FAIL] Application metrics could not be read: {exc}")
            return self.fail("metrics_unreadable", error=f"{type(exc).__name__}: {exc}")

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
        if healthy:
            return None
        # Behavioural, and measured only after cluster shape checked out above.
        # The caller relabels this for the post-replay window.
        return self.fail(
            "traffic_did_not_recover",
            rate=round(observed.actual_rate, 2),
            success_rate=round(observed.success_rate, 3),
            attempts_per_request=None if amplification == float("inf") else round(amplification, 2),
            queue_depth=queue_depth,
        )

    def _wait_for_healthy_state(self, timeout_seconds: float) -> dict | None:
        """Return None once a sample is healthy, else the last sample's verdict."""
        deadline = time.monotonic() + timeout_seconds
        last = None
        while time.monotonic() < deadline:
            last = self._unhealthy_sample()
            if last is None:
                return None
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.poll_interval_seconds, remaining))
        return last if last is not None else self.fail("traffic_did_not_recover")

    def _runtime_policy_outside_safe_bounds(self) -> dict | None:
        """Reject recovery obtained by effectively disabling backpressure."""
        try:
            metrics = self.problem.workload.metrics.snapshot()
        except Exception as exc:
            print(f"[FAIL] The effective rate policy could not be read: {exc}")
            return self.fail("metrics_unreadable", error=f"{type(exc).__name__}: {exc}")

        qps_limit = metrics.get("rate_backend_qps_limit")
        queue_capacity = metrics.get("rate_queue_capacity")
        if qps_limit is None or queue_capacity is None:
            print("[FAIL] The rate service did not expose its effective admission-control policy")
            return self.fail("policy_not_exposed")

        maximum_qps = self.problem.maximum_safe_backend_qps_limit
        maximum_queue = self.problem.maximum_safe_queue_capacity
        if not 0 < qps_limit <= maximum_qps:
            print(
                "[FAIL] The effective backend QPS limit is outside the safe operating envelope: "
                f"{qps_limit:g} (maximum {maximum_qps:g})"
            )
            return self.fail("qps_limit_outside_safe_envelope", qps_limit=qps_limit, maximum=maximum_qps)
        if not 0 < queue_capacity <= maximum_queue:
            print(
                "[FAIL] The effective queue capacity is outside the safe operating envelope: "
                f"{queue_capacity:g} (maximum {maximum_queue:g})"
            )
            return self.fail(
                "queue_capacity_outside_safe_envelope",
                queue_capacity=queue_capacity,
                maximum=maximum_queue,
            )

        print(f"[Policy] backend_qps_limit={qps_limit:g} queue_capacity={queue_capacity:g}")
        return None

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
            # capture_baseline is the conductor's to call, so an empty baseline
            # is our sequencing failure and says nothing about the agent.
            return self.fail("baseline_not_captured")

        try:
            self.problem.workload.start()
            self.problem.workload.set_rate(self.problem.base_rate)

            outside_bounds = self._runtime_policy_outside_safe_bounds()
            if outside_bounds is not None:
                return outside_bounds

            unhealthy = self._wait_for_healthy_state(self.initial_recovery_timeout_seconds)
            if unhealthy is not None:
                print("[FAIL] Normal search traffic did not recover")
                return unhealthy

            if not self._replay_trigger():
                print("[FAIL] The protected trigger workload was not delivered at the required rate")
                return self.fail("trigger_load_not_delivered", trigger_rate=self.problem.trigger_rate)

            unhealthy = self._wait_for_healthy_state(self.replay_recovery_timeout_seconds)
            if unhealthy is not None:
                print("[FAIL] The application did not recover after the temporary trigger ended")
                # Relabel only a behavioural verdict: recovering before the
                # replay and not after is a different finding from never
                # recovering. An environmental verdict keeps its own reason,
                # since the window it happened in does not change whose fault
                # it was.
                if unhealthy.get("reason") == "traffic_did_not_recover":
                    return self.fail("did_not_recover_after_trigger", **unhealthy.get("detail", {}))
                return unhealthy

            outside_bounds = self._runtime_policy_outside_safe_bounds()
            if outside_bounds is not None:
                return outside_bounds

            unhealthy = self._cluster_shape_unhealthy()
            if unhealthy is not None:
                return unhealthy
            self.problem.workload.stop()
        except Exception as exc:
            print(f"[FAIL] Error while verifying mitigation: {exc}")
            return self.fail_from_exception(exc)

        print("[PASS] Normal traffic is healthy and recovers after replaying the temporary trigger")
        return {"success": True}
