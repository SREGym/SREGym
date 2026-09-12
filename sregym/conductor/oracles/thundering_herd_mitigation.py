"""Behavioral mitigation oracle for duplicated in-flight catalog RPCs."""

from __future__ import annotations

import time

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.prometheus_query import catalog_list_products_total
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
        "hardcoded_recommendations": FailureClass.AGENT_ERROR,
        "slo_not_met": FailureClass.AGENT_ERROR,
        "insufficient_samples": FailureClass.AMBIGUOUS,
        "empty_catalog": FailureClass.ENVIRONMENT_ERROR,
        "baseline_not_captured": FailureClass.HARNESS_ERROR,
    }

    # Offered load is owned by this oracle. Hidden-wave constants must not appear
    # in the problem root_cause text.
    visible_concurrency = 8
    hidden_concurrency = 24
    wave_seconds = 25.0
    scrape_wait_seconds = 45.0
    poll_interval_seconds = 5.0
    seed_product_ids = ("OLJCESPC7Z",)
    hidden_product_ids = ("66VCHSJNUP",)

    max_amplification = 2.0
    min_fault_amplification = 6.0
    min_success_rate = 0.90
    max_p95_seconds = 5.0
    max_p99_seconds = 8.0
    min_completed_per_worker = 1

    def __init__(self, problem):
        super().__init__(problem)
        self._baseline_deployments: set[str] = set()
        self._baseline_replicas: dict[str, int] = {}
        self._baseline_shape: dict[str, dict] = {}

    def capture_baseline(self) -> None:
        deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(
            namespace=self.problem.namespace
        )
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
            name: self._fingerprint_deployment(current[name])
            for name in self.guarded_deployments
            if name in current
        }
        print(
            f"[Baseline] {len(self._baseline_deployments)} deployments; "
            f"guarded={', '.join(self.guarded_deployments)}"
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
        if not self._baseline_deployments:
            print("[FAIL] No healthy baseline was captured")
            return self.fail("baseline_not_captured")
        try:
            deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(
                namespace=self.problem.namespace
            )
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
            deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(
                namespace=self.problem.namespace
            )
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
                print(
                    f"[FAIL] Deployment {name!r} replicas changed "
                    f"({expected_replicas} -> {observed_replicas})"
                )
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
                print(
                    f"[FAIL] Deployment {name!r} replicas changed "
                    f"({expected['replicas']} -> {observed['replicas']})"
                )
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

    def _amplification(self, catalog_delta: float, succeeded: int) -> float:
        if succeeded <= 0:
            return float("inf")
        return catalog_delta / succeeded

    def _wave_failure(
        self,
        snapshot: HerdSnapshot,
        amplification: float,
        catalog_ids: set[str],
        *,
        concurrency: int,
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
        if snapshot.succeeded >= 5 and amplification <= 0:
            print("[FAIL] Catalog ListProducts did not increase for successful recommendations")
            return self.fail("prometheus_unreachable")
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
        if snapshot.succeeded >= 8 and snapshot.distinct_recommendation_sets < 2:
            print("[FAIL] Recommendations look hard-coded (the returned ID set never changed)")
            return self.fail("hardcoded_recommendations")
        return None

    def _wave_healthy(
        self,
        snapshot: HerdSnapshot,
        amplification: float,
        catalog_ids: set[str],
        *,
        concurrency: int,
    ) -> bool:
        return self._wave_failure(snapshot, amplification, catalog_ids, concurrency=concurrency) is None

    def _run_wave(self, *, concurrency: int, product_ids: tuple[str, ...]) -> tuple[HerdSnapshot, float] | None:
        before = self._catalog_list_products_total()
        if before is None:
            return None
        snapshot = self.problem.workload.run(
            concurrency=concurrency,
            duration_seconds=self.wave_seconds,
            product_ids=product_ids,
        )
        after = before
        waited = 0.0
        while waited < self.scrape_wait_seconds:
            time.sleep(self.poll_interval_seconds)
            waited += self.poll_interval_seconds
            sample = self._catalog_list_products_total()
            if sample is None:
                return None
            after = sample
            if after > before:
                break
        print(
            f"[Prom] ListProducts before={before:.0f} after={after:.0f} "
            f"delta={after - before:.0f} waited={waited:.0f}s"
        )
        return snapshot, self._amplification(after - before, snapshot.succeeded)

    def assert_fault_present(self) -> None:
        """Fail injection if the overlay is not multiplying catalog RPCs."""
        self.problem.workload.start()
        try:
            measured = self._run_wave(
                concurrency=self.visible_concurrency,
                product_ids=self.seed_product_ids,
            )
            if measured is None:
                raise RuntimeError("catalog RPC metrics were not available while verifying the fault")
            snapshot, amplification = measured
            print(
                "[Fault] "
                f"completed={snapshot.completed} success={snapshot.success_rate:.1%} "
                f"amplification={amplification:.2f}"
            )
            if snapshot.succeeded < 5:
                raise RuntimeError("fault verification did not complete enough recommendations")
            if amplification < self.min_fault_amplification:
                raise RuntimeError(
                    f"catalog amplification was {amplification:.2f}; "
                    f"expected at least {self.min_fault_amplification:.2f}"
                )
        finally:
            self.problem.workload.stop()

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Thundering Herd Mitigation Evaluation ==")
        unhealthy = self._cluster_shape_unhealthy()
        if unhealthy is not None:
            return unhealthy
        capacity = self._capacity_changed()
        if capacity is not None:
            return capacity

        try:
            self.problem.workload.start()
            catalog_ids = self.problem.workload.catalog_product_ids()
            if not catalog_ids:
                print("[FAIL] The product catalog API returned no product IDs")
                return self.fail("empty_catalog")

            first = self._run_wave(
                concurrency=self.visible_concurrency,
                product_ids=self.seed_product_ids,
            )
            if first is None:
                return self.fail("prometheus_unreachable")
            snapshot, amplification = first
            wave_fail = self._wave_failure(
                snapshot, amplification, catalog_ids, concurrency=self.visible_concurrency
            )
            if wave_fail is not None:
                return wave_fail

            second = self._run_wave(
                concurrency=self.hidden_concurrency,
                product_ids=self.hidden_product_ids,
            )
            if second is None:
                return self.fail("prometheus_unreachable")
            hidden_snapshot, hidden_amplification = second
            wave_fail = self._wave_failure(
                hidden_snapshot,
                hidden_amplification,
                catalog_ids,
                concurrency=self.hidden_concurrency,
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
            self.problem.workload.stop()

        print("[PASS] Catalog amplification dropped and recommendations stayed correct")
        return {"success": True}
