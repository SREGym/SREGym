"""Behavioral mitigation oracle for duplicated in-flight catalog RPCs."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.prometheus_query import catalog_list_products_total, list_recommendations_total
from sregym.generators.workload.recommendation_herd import HerdSnapshot
from sregym.service.rollout import deployment_rollout_complete


class ThunderingHerdMitigationOracle(Oracle):
    """Require unamplified, correct recommendations without changing capacity."""

    importance = 1.0
    guarded_deployments = ("recommendation", "product-catalog", "frontend")
    required_services = ("recommendation", "product-catalog", "frontend")

    FAILURE_CLASSES = {
        "required_deployment_missing": FailureClass.AGENT_ERROR,
        "capacity_changed": FailureClass.AGENT_ERROR,
        "invalid_recommendation_ids": FailureClass.AGENT_ERROR,
        "slo_not_met": FailureClass.AGENT_ERROR,
        "insufficient_samples": FailureClass.AMBIGUOUS,
        "empty_catalog": FailureClass.ENVIRONMENT_ERROR,
        "baseline_not_captured": FailureClass.HARNESS_ERROR,
    }

    # Offered load is owned by this oracle. Hidden-wave constants must not appear
    # in the problem root_cause text.
    # Conservative bursts plus the workload's rate cap avoid saturating a
    # healthy catalog on small KIND hosts before the fault is injected.
    fault_concurrency = 2
    visible_concurrency = 4
    hidden_concurrency = 6
    wave_seconds = 25.0
    scrape_wait_seconds = 45.0
    poll_interval_seconds = 5.0
    wave_gap_seconds = 5.0
    seed_product_ids = ("OLJCESPC7Z",)
    hidden_product_ids = ("66VCHSJNUP",)

    max_amplification = 2.0
    min_fault_amplification = 6.0
    overlay_log_marker = "recommendation catalog refetch"
    min_success_rate = 0.90
    max_p95_seconds = 8.0
    max_p99_seconds = 12.0
    min_completed_per_worker = 1
    evaluation_timeout_seconds = 360.0

    def __init__(self, problem):
        super().__init__(problem)
        self._baseline_deployments: set[str] = set()
        self._baseline_replicas: dict[str, int] = {}
        self._baseline_shape: dict[str, dict] = {}

    def capture_baseline(self) -> None:
        deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(namespace=self.problem.namespace)
        current = {deployment.metadata.name: deployment for deployment in deployments.items}
        self._baseline_deployments = set(current)
        self._baseline_replicas = {
            name: (deployment.spec.replicas if deployment.spec.replicas is not None else 1)
            for name, deployment in current.items()
        }
        missing_guarded = [name for name in self.guarded_deployments if name not in current]
        if missing_guarded:
            print(f"[FAIL] Baseline is missing guarded Deployments: {', '.join(missing_guarded)}")
        self._baseline_shape = {
            name: self._fingerprint_deployment(current[name]) for name in self.guarded_deployments if name in current
        }
        print(
            f"[Baseline] {len(self._baseline_deployments)} deployments; guarded={', '.join(self.guarded_deployments)}"
        )

    @staticmethod
    def _rollout_complete(deployment) -> bool:
        return deployment_rollout_complete(deployment)

    @staticmethod
    def _fingerprint_deployment(deployment) -> dict:
        containers = {}
        for container in deployment.spec.template.spec.containers:
            resources = container.resources
            requests = dict(getattr(resources, "requests", None) or {}) if resources else {}
            limits = dict(getattr(resources, "limits", None) or {}) if resources else {}
            containers[container.name] = {
                "cpu_request": str(requests.get("cpu", "")),
                "memory_request": str(requests.get("memory", "")),
                "cpu_limit": str(limits.get("cpu", "")),
                "memory_limit": str(limits.get("memory", "")),
            }
        replicas = deployment.spec.replicas if deployment.spec.replicas is not None else 1
        return {"replicas": replicas, "containers": containers}

    def _cluster_shape_unhealthy(self) -> dict | None:
        if not self._baseline_deployments or not set(self.guarded_deployments).issubset(self._baseline_deployments):
            print("[FAIL] No healthy baseline was captured")
            return self.fail("baseline_not_captured")
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

    def _cluster_shape_healthy(self) -> bool:
        return self._cluster_shape_unhealthy() is None

    def _capacity_changed(self) -> dict | None:
        try:
            deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(namespace=self.problem.namespace)
            current = {deployment.metadata.name: deployment for deployment in deployments.items}
        except ApiException as exc:
            print(f"[FAIL] Could not read Deployments for resource comparison: {exc}")
            return self.fail_from_exception(exc)
        for name, expected_replicas in self._baseline_replicas.items():
            if name not in current:
                print(f"[FAIL] Deployment {name!r} is missing")
                return self.fail("required_deployment_missing", deployments=[name])
            observed_replicas = current[name].spec.replicas
            if observed_replicas is None:
                observed_replicas = 1
            if observed_replicas != expected_replicas:
                print(f"[FAIL] Deployment {name!r} replicas changed ({expected_replicas} -> {observed_replicas})")
                return self.fail(
                    "capacity_changed",
                    deployment=name,
                    expected_replicas=expected_replicas,
                    observed_replicas=observed_replicas,
                )
        for name, expected in self._baseline_shape.items():
            if name not in current:
                print(f"[FAIL] Guarded Deployment {name!r} is missing")
                return self.fail("required_deployment_missing", deployments=[name])
            observed = self._fingerprint_deployment(current[name])
            if observed["replicas"] != expected["replicas"]:
                print(f"[FAIL] Deployment {name!r} replicas changed ({expected['replicas']} -> {observed['replicas']})")
                return self.fail(
                    "capacity_changed",
                    deployment=name,
                    expected_replicas=expected["replicas"],
                    observed_replicas=observed["replicas"],
                )
            if observed["containers"] != expected["containers"]:
                print(f"[FAIL] Deployment {name!r} CPU/memory requests or limits changed")
                return self.fail("capacity_changed", deployment=name)
        return None

    def _resources_unchanged(self) -> bool:
        return self._capacity_changed() is None

    def _catalog_list_products_total(self) -> float | None:
        return catalog_list_products_total(self.problem.namespace)

    def _list_recommendations_total(self) -> float | None:
        return list_recommendations_total(self.problem.namespace)

    def _rpc_amplification(self, product_delta: float, recommendation_delta: float) -> float | None:
        if (
            math.isfinite(product_delta)
            and math.isfinite(recommendation_delta)
            and recommendation_delta > 0
            and product_delta >= 0
        ):
            return product_delta / recommendation_delta
        # HTTP responses are not a valid denominator for an RPC amplification
        # ratio. Missing recommendation spans must fail closed rather than make
        # a partial product-catalog export look healthy.
        return None

    def _wave_failure(
        self,
        snapshot: HerdSnapshot,
        amplification: float,
        catalog_ids: set[str],
        *,
        concurrency: int,
        product_ids: tuple[str, ...],
    ) -> dict | None:
        minimum_completed = max(5, concurrency * self.min_completed_per_worker)
        print(
            "[Health] "
            f"completed={snapshot.completed} success={snapshot.success_rate:.1%} "
            f"p95={snapshot.p95_latency_seconds} p99={snapshot.p99_latency_seconds} "
            f"amplification={amplification:.2f} distinct_sets={snapshot.distinct_recommendation_sets}"
        )
        if snapshot.completed < minimum_completed:
            print(f"[FAIL] Too few completed recommendation requests ({snapshot.completed})")
            return self.fail("insufficient_samples", completed=snapshot.completed)
        if snapshot.success_rate < self.min_success_rate:
            print("[FAIL] Recommendation success rate is below the SLO")
            return self.fail("slo_not_met", success_rate=snapshot.success_rate)
        if snapshot.p95_latency_seconds is None or snapshot.p95_latency_seconds > self.max_p95_seconds:
            print("[FAIL] Recommendation p95 latency is above the SLO")
            return self.fail("slo_not_met", p95=snapshot.p95_latency_seconds)
        if snapshot.p99_latency_seconds is not None and snapshot.p99_latency_seconds > self.max_p99_seconds:
            print("[FAIL] Recommendation p99 latency is above the SLO")
            return self.fail("slo_not_met", p99=snapshot.p99_latency_seconds)
        if amplification > self.max_amplification:
            print(
                f"[FAIL] Catalog amplification is {amplification:.2f} "
                f"(maximum {self.max_amplification:.2f} ListProducts per useful recommendation)"
            )
            return self.fail("fault_still_present", amplification=round(amplification, 2))
        if not snapshot.product_ids:
            print("[FAIL] Recommendations returned no product IDs")
            return self.fail("invalid_recommendation_ids")
        unknown = [item for item in snapshot.product_ids if item not in catalog_ids]
        if unknown:
            print(f"[FAIL] Recommendations returned IDs that are not in the catalog: {unknown}")
            return self.fail("invalid_recommendation_ids", unknown=unknown)
        excluded = sorted(set(snapshot.product_ids).intersection(product_ids))
        if excluded:
            print(f"[FAIL] Recommendations returned requested exclusions: {excluded}")
            return self.fail("invalid_recommendation_ids", excluded=excluded)
        return None

    def _wave_healthy(
        self,
        snapshot: HerdSnapshot,
        amplification: float,
        catalog_ids: set[str],
        *,
        concurrency: int,
        product_ids: tuple[str, ...] = (),
    ) -> bool:
        return (
            self._wave_failure(snapshot, amplification, catalog_ids, concurrency=concurrency, product_ids=product_ids)
            is None
        )

    def _run_wave(
        self, *, concurrency: int, product_ids: tuple[str, ...], allow_missing_baseline: bool = False
    ) -> tuple[HerdSnapshot, float | None, float | None] | None:
        before_products = self._catalog_list_products_total()
        before_recs = self._list_recommendations_total()
        if before_products is None or before_recs is None:
            if not allow_missing_baseline:
                return None
            # A fresh deployment has no span series until its first requests.
            # Injection owns that first traffic; grading still requires an
            # observed baseline so missing telemetry cannot pass a repair.
            before_products = before_products if before_products is not None else 0.0
            before_recs = before_recs if before_recs is not None else 0.0
        wave_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        snapshot = self.problem.workload.run(
            concurrency=concurrency,
            duration_seconds=self.wave_seconds,
            product_ids=product_ids,
        )
        after_products = before_products
        after_recs = before_recs
        waited = 0.0
        # Wait for the complete export window: the counters can arrive on
        # different scrapes, and a warm cache need not move ListProducts.
        while waited < self.scrape_wait_seconds:
            time.sleep(self.poll_interval_seconds)
            waited += self.poll_interval_seconds
            sample_products = self._catalog_list_products_total()
            sample_recs = self._list_recommendations_total()
            if sample_products is None or sample_recs is None:
                if allow_missing_baseline and waited < self.scrape_wait_seconds:
                    continue
                return None
            after_products = sample_products
            after_recs = sample_recs
        product_delta = after_products - before_products
        rec_delta = after_recs - before_recs
        amplification = self._rpc_amplification(product_delta, rec_delta)
        amplification_text = f"{amplification:.2f}" if amplification is not None else "unavailable"
        print(
            "[Prom] "
            f"ListProducts delta={product_delta:.0f} ListRecommendations delta={rec_delta:.0f} "
            f"amplification={amplification_text} waited={waited:.0f}s"
        )
        log_amplification = self._overlay_log_amplification(snapshot.succeeded, since_time=wave_started_at)
        return snapshot, amplification, log_amplification

    def _overlay_log_amplification(self, succeeded: int, *, since_time: str) -> float | None:
        if succeeded <= 0:
            return None
        deployment = getattr(self.problem, "recommendation_deployment", "recommendation")
        commands = (
            (
                f"kubectl logs -n {self.problem.namespace} "
                f"-l app.kubernetes.io/component={deployment} -c {deployment} "
                f"--since-time={since_time} --tail=-1 --max-log-requests=20"
            ),
            (
                f"kubectl logs -n {self.problem.namespace} deploy/{deployment} "
                f"-c {deployment} --since-time={since_time} --tail=-1"
            ),
        )
        logs = ""
        last_error = None
        for command in commands:
            try:
                logs = self.problem.kubectl.exec_command_checked(command, timeout=20)
                if logs.strip() or command == commands[-1]:
                    break
            except (AttributeError, RuntimeError) as exc:
                last_error = exc
        else:
            print(f"[Fault] recommendation logs unavailable: {last_error}")
            return None
        refetches = logs.count(self.overlay_log_marker)
        print(f"[Fault] log refetch={refetches} succeeded={succeeded}")
        return refetches / succeeded

    def assert_fault_present(self) -> None:
        """Fail injection if the overlay is not multiplying catalog RPCs."""
        self.problem.workload.start()
        try:
            measured = self._run_wave(
                concurrency=self.fault_concurrency,
                product_ids=self.seed_product_ids,
                allow_missing_baseline=True,
            )
            if measured is None:
                raise RuntimeError("catalog RPC metrics were not available while verifying the fault")
            snapshot, amplification, log_amplification = measured
            observed_values = [value for value in (amplification, log_amplification) if value is not None]
            if not observed_values:
                raise RuntimeError("catalog RPC metrics and recommendation logs were not available")
            observed = max(observed_values)
            amplification_text = f"{amplification:.2f}" if amplification is not None else "unavailable"
            print(
                "[Fault] "
                f"completed={snapshot.completed} success={snapshot.success_rate:.1%} "
                f"amplification={amplification_text} log_amplification={log_amplification}"
            )
            if snapshot.succeeded < 5:
                raise RuntimeError("fault verification did not complete enough recommendations")
            if observed < self.min_fault_amplification:
                raise RuntimeError(
                    f"catalog amplification was {observed:.2f}; expected at least {self.min_fault_amplification:.2f}"
                )
        finally:
            self.problem.workload.stop()

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Thundering Herd Mitigation Evaluation ==")
        resume_traffic = self.problem.workload.background_running
        try:
            self.problem.workload.stop_background()
            unhealthy = self._cluster_shape_unhealthy()
            if unhealthy is not None:
                return unhealthy
            capacity = self._capacity_changed()
            if capacity is not None:
                return capacity
            self.problem.workload.start()
            catalog_ids = self.problem.workload.catalog_product_ids()
            if not catalog_ids:
                print("[FAIL] The product catalog API returned no product IDs")
                return self.fail("empty_catalog")

            # Flush investigation traffic and the catalog lookup before taking
            # the baseline for the current workload's RPC deltas.
            time.sleep(self.scrape_wait_seconds)
            first = self._run_wave(
                concurrency=self.visible_concurrency,
                product_ids=self.seed_product_ids,
            )
            if first is None:
                return self.fail("prometheus_unreachable")
            snapshot, amplification, _ = first
            if amplification is None:
                return self.fail("prometheus_unreachable")
            # Repairs can retain the overlay's debug messages while serving
            # real cached catalog data. Grade actual RPCs; logs only provide
            # diagnostic and injection evidence.
            wave_fail = self._wave_failure(
                snapshot,
                amplification,
                catalog_ids,
                concurrency=self.visible_concurrency,
                product_ids=self.seed_product_ids,
            )
            if wave_fail is not None:
                return wave_fail

            time.sleep(self.wave_gap_seconds)
            # Challenge a fixed responder with IDs it actually returned, while
            # keeping at least one catalog product eligible for recommendation.
            hidden_exclusions = list(dict.fromkeys(self.hidden_product_ids))
            for product_id in sorted(snapshot.product_ids):
                candidate = (*hidden_exclusions, product_id)
                if len(catalog_ids.difference(candidate)) > 0 and product_id not in hidden_exclusions:
                    hidden_exclusions.append(product_id)
            hidden_product_ids = tuple(hidden_exclusions)
            second = self._run_wave(
                concurrency=self.hidden_concurrency,
                product_ids=hidden_product_ids,
            )
            if second is None:
                return self.fail("prometheus_unreachable")
            hidden_snapshot, hidden_amplification, _ = second
            if hidden_amplification is None:
                return self.fail("prometheus_unreachable")
            wave_fail = self._wave_failure(
                hidden_snapshot,
                hidden_amplification,
                catalog_ids,
                concurrency=self.hidden_concurrency,
                product_ids=hidden_product_ids,
            )
            if wave_fail is not None:
                return wave_fail

            unhealthy = self._cluster_shape_unhealthy()
            if unhealthy is not None:
                return unhealthy
            capacity = self._capacity_changed()
            if capacity is not None:
                return capacity
        except Exception as exc:
            print(f"[FAIL] Error while verifying mitigation: {exc}")
            return self.fail_from_exception(exc)
        finally:
            if resume_traffic:
                self.problem.start_workload()
            else:
                self.problem.workload.stop()

        print("[PASS] Catalog amplification dropped and recommendations stayed correct")
        return {"success": True}
