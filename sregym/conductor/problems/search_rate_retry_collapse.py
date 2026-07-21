"""Metastable search-to-rate retry amplification in Hotel Reservation."""

from __future__ import annotations

import time

from kubernetes import client

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.search_rate_retry_mitigation import SearchRateRetryMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.workload.hotel_search import HotelSearchWorkload
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class SearchRateRetryCollapse(Problem):
    """Create a queue/timeout/retry feedback loop with a bounded trigger."""

    run_default_workload = False

    search_deployment = "search"
    search_container = "hotel-reserv-search"
    rate_deployment = "rate"
    rate_container = "hotel-reserv-rate"

    base_rate = 8.0
    trigger_rate = 40.0
    trigger_seconds = 10.0
    backend_qps_limit = 20
    queue_capacity = 256
    rpc_timeout_ms = 750
    rpc_max_attempts = 3
    rpc_initial_backoff_ms = 50
    rpc_backoff_multiplier = 2.0
    rpc_jitter = 0.2

    mitigated_backend_qps_limit = 50
    mitigated_rpc_timeout_ms = 1000
    mitigated_rpc_max_attempts = 2

    # The standard application policy is 500 QPS with a 256-entry queue.
    # Recovery may tune below those values, but must not disable backpressure
    # or trade the outage for an unbounded memory/latency risk.
    maximum_safe_backend_qps_limit = 500
    maximum_safe_queue_capacity = 256

    baseline_warmup_seconds = 20
    baseline_measure_seconds = 20
    post_trigger_first_seconds = 20
    post_trigger_second_seconds = 25

    def __init__(self):
        super().__init__(
            app=HotelReservation(
                mount_failure_scripts=False,
                deployment_env_overrides=self._vulnerable_deployment_env(),
            )
        )
        self.kubectl = KubeCtl()
        self.workload = HotelSearchWorkload(self.namespace, base_rate=self.base_rate)
        self._injection_attempted = False
        self.root_cause = self.build_structured_root_cause(
            component="search-to-rate RPC path",
            namespace=self.namespace,
            description=(
                "The search service's rate RPC policy permits three attempts with a short per-attempt deadline. "
                "After a transient traffic burst fills the rate service's bounded backend queue, calls exceed "
                "their deadline but continue consuming downstream capacity. Search retries those expired calls, "
                "so internal rate requests remain above backend capacity even after external traffic returns to "
                "normal. The sustaining cause is the timeout/retry/queue feedback loop, not the expired burst. "
                "A diagnosis that only calls the static rate limit too low under normal traffic is incomplete: "
                "normal traffic is below that limit and the bounded trigger plus post-trigger amplification are "
                "required parts of the causal explanation."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(
            problem=self,
            expected=self.root_cause,
            minimum_dimension_scores={"D2": 0.67},
        )
        self.mitigation_oracle = SearchRateRetryMitigationOracle(problem=self)

    @classmethod
    def _vulnerable_deployment_env(cls) -> dict[str, dict[str, dict[str, str]]]:
        return {
            cls.rate_deployment: {
                cls.rate_container: {
                    "RATE_BACKEND_QPS_LIMIT": str(cls.backend_qps_limit),
                    "RATE_QUEUE_CAPACITY": str(cls.queue_capacity),
                }
            },
            cls.search_deployment: {
                cls.search_container: {
                    "RATE_RPC_TIMEOUT_MS": str(cls.rpc_timeout_ms),
                    "RATE_RPC_MAX_ATTEMPTS": str(cls.rpc_max_attempts),
                    "RATE_RPC_INITIAL_BACKOFF_MS": str(cls.rpc_initial_backoff_ms),
                    "RATE_RPC_BACKOFF_MULTIPLIER": str(cls.rpc_backoff_multiplier),
                    "RATE_RPC_JITTER": str(cls.rpc_jitter),
                }
            },
        }

    def _replace_container_env(self, deployment_name: str, container_name: str, values: dict[str, str]) -> None:
        deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
        template = deployment.spec.template
        container = next((item for item in template.spec.containers if item.name == container_name), None)
        if container is None:
            raise RuntimeError(f"container {container_name!r} not found in deployment/{deployment_name}")

        preserved = [item for item in (container.env or []) if item.name not in values]
        injected = [client.V1EnvVar(name=name, value=str(value)) for name, value in values.items()]
        container.env = [*preserved, *injected]
        deployment.spec.template = template
        self.kubectl.apps_v1_api.replace_namespaced_deployment(
            name=deployment_name,
            namespace=self.namespace,
            body=deployment,
        )

    def _apply_mitigated_policy(self) -> None:
        self._replace_container_env(
            self.rate_deployment,
            self.rate_container,
            {
                "RATE_BACKEND_QPS_LIMIT": str(self.mitigated_backend_qps_limit),
                "RATE_QUEUE_CAPACITY": str(self.queue_capacity),
            },
        )
        self._replace_container_env(
            self.search_deployment,
            self.search_container,
            {
                "RATE_RPC_TIMEOUT_MS": str(self.mitigated_rpc_timeout_ms),
                "RATE_RPC_MAX_ATTEMPTS": str(self.mitigated_rpc_max_attempts),
                "RATE_RPC_INITIAL_BACKOFF_MS": str(self.rpc_initial_backoff_ms),
                "RATE_RPC_BACKOFF_MULTIPLIER": str(self.rpc_backoff_multiplier),
                "RATE_RPC_JITTER": str(self.rpc_jitter),
            },
        )

    def _wait_for_rollouts(self) -> None:
        for name in (self.rate_deployment, self.search_deployment):
            self.kubectl.exec_command_checked(
                f"kubectl rollout status deployment/{name} -n {self.namespace} --timeout=300s"
            )
        self.kubectl.wait_for_ready(
            self.namespace,
            service_names=[self.rate_deployment, self.search_deployment],
        )

    @staticmethod
    def _delta(before: dict[str, float], after: dict[str, float], name: str) -> float:
        return after.get(name, 0.0) - before.get(name, 0.0)

    def _establish_healthy_vulnerable_baseline(self) -> None:
        print(f"[Baseline] Warming the application at {self.base_rate:.0f} requests/s...")
        time.sleep(self.baseline_warmup_seconds)
        before = self.workload.metrics.snapshot()
        time.sleep(self.baseline_measure_seconds)
        after = self.workload.metrics.snapshot()
        observed = self.workload.snapshot(self.baseline_measure_seconds)

        search_requests = self._delta(before, after, "search_requests_total")
        attempts = self._delta(before, after, "search_rate_attempts_total")
        amplification = attempts / search_requests if search_requests > 0 else float("inf")
        queue_depth = after.get("rate_queue_depth", -1)
        print(
            "[Baseline] "
            f"rate={observed.actual_rate:.2f}/s success={observed.success_rate:.1%} "
            f"attempts/request={amplification:.2f} queue={queue_depth:.0f}"
        )

        failures = []
        if observed.actual_rate < self.base_rate * 0.85:
            failures.append("the protected workload did not reach its baseline rate")
        if observed.completed < self.base_rate * self.baseline_measure_seconds * 0.75:
            failures.append("too few baseline requests completed")
        if observed.success_rate < 0.90:
            failures.append("baseline functional success was below 90%")
        if amplification > 1.20:
            failures.append("the vulnerable retry policy was already amplifying healthy traffic")
        if queue_depth < 0 or queue_depth > 5:
            failures.append("the rate queue was not empty during the healthy baseline")
        if failures:
            raise RuntimeError("healthy vulnerable baseline was not established: " + "; ".join(failures))

    def _apply_trigger_and_verify_sustaining_loop(self) -> None:
        print(f"[Trigger] Raising external search traffic to {self.trigger_rate:.0f} requests/s...")
        self.workload.set_rate(self.trigger_rate)
        try:
            time.sleep(self.trigger_seconds)
            trigger_observed = self.workload.snapshot(self.trigger_seconds)
        finally:
            self.workload.set_rate(self.base_rate)
        if trigger_observed.actual_rate < self.trigger_rate * 0.80:
            raise RuntimeError(
                f"temporary trigger only reached {trigger_observed.actual_rate:.2f} requests/s; "
                f"expected at least {self.trigger_rate * 0.80:.2f}"
            )
        print(f"[Trigger] Ended after {self.trigger_seconds:.0f}s; external traffic returned to baseline")

        time.sleep(self.post_trigger_first_seconds)
        middle = self.workload.metrics.snapshot()
        middle_queue = middle.get("rate_queue_depth", -1)
        time.sleep(self.post_trigger_second_seconds)
        after = self.workload.metrics.snapshot()
        observed = self.workload.snapshot(min(20.0, self.post_trigger_second_seconds))

        search_requests = self._delta(middle, after, "search_requests_total")
        attempts = self._delta(middle, after, "search_rate_attempts_total")
        amplification = attempts / search_requests if search_requests > 0 else float("inf")
        final_queue = after.get("rate_queue_depth", -1)
        print(
            "[Post-trigger] "
            f"rate={observed.actual_rate:.2f}/s success={observed.success_rate:.1%} "
            f"attempts/request={amplification:.2f} queue={middle_queue:.0f}->{final_queue:.0f}"
        )

        failures = []
        if not self.base_rate * 0.80 <= observed.actual_rate <= self.base_rate * 1.20:
            failures.append("external traffic did not return to the original baseline")
        if final_queue < 20:
            failures.append("the downstream queue did not remain backlogged")
        if final_queue + 5 < middle_queue:
            failures.append("the downstream queue was recovering without mitigation")
        if amplification < 2.0:
            failures.append("internal rate calls were not retry-amplified")
        if observed.success_rate > 0.50:
            failures.append("the user-visible failure did not persist after the trigger")
        if failures:
            raise RuntimeError("metastable state was not established: " + "; ".join(failures))

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        if self.fault_injected or self._injection_attempted:
            raise RuntimeError("fault injection is already active")
        self._injection_attempted = True
        try:
            self.workload.start()
            self._establish_healthy_vulnerable_baseline()
            self._apply_trigger_and_verify_sustaining_loop()
        except Exception:
            self.workload.stop()
            try:
                self._apply_mitigated_policy()
                self._wait_for_rollouts()
            except Exception as cleanup_error:
                print(f"[Cleanup] Failed to apply the safe runtime policy: {cleanup_error}")
            raise

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        try:
            self._apply_mitigated_policy()
            self._wait_for_rollouts()
        finally:
            self.workload.stop()

    def stop_workload(self):
        self.workload.stop()
