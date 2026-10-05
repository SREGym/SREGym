"""Duplicated in-flight ListProducts calls from Astronomy Shop recommendation.

Calibrated and supported on x86-64 only.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.thundering_herd_mitigation import ThunderingHerdMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.workload.recommendation_herd import RecommendationHerdWorkload
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

_ASSET = Path(__file__).parent / "assets" / "thundering_herd_cascade_recommendation.py"
_VALUES = Path(__file__).parent / "assets" / "thundering_herd_values.yaml"
_SOURCE_CONFIGMAP = "recommendation-code"
_SOURCE_PATH = "/app/recommendation_server.py"

ROOT_CAUSE_DESCRIPTION = (
    "The recommendation service issues about ten equivalent ListProducts RPCs "
    "for each ListRecommendations: initial catalog discovery is followed by a "
    "full catalog reload for each eligible candidate lookup, with no reuse or "
    "coalescing. Concurrent "
    "clients therefore multiply catalog load; this is duplicated in-flight work, "
    "not a retry storm. Pods stay Running. A diagnosis that blames product-catalog "
    "alone is incomplete: the catalog is healthy and the caller is multiplying work. "
    "Calibrated and supported on x86-64 only."
)


class _RecommendationSourceApp(AstronomyShop):
    """Use the editable source package in the healthy deployment as well."""

    extra_values_files = (_VALUES,)

    def deploy(self):
        super().deploy()
        self.prepare_source_package()

    def prepare_source_package(self):
        source = self.kubectl.exec_command_checked(
            f"kubectl exec -n {self.namespace} deploy/recommendation -c recommendation -- cat {_SOURCE_PATH}"
        )
        if not source.strip():
            raise RuntimeError("recommendation source package is empty")
        ApplicationFaultInjector(namespace=self.namespace).inject_source_file_override(
            deployment_name="recommendation",
            source_path=_SOURCE_PATH,
            replacement_content=source,
            configmap_name=_SOURCE_CONFIGMAP,
            container_name="recommendation",
        )
        _wait_for_services(self.kubectl, self.namespace, ["recommendation", "product-catalog"])


def _wait_for_services(kubectl, namespace: str, services: list[str]) -> None:
    """Wait for active endpoints and for previous source revisions to drain."""
    kubectl.wait_for_ready(namespace, service_names=services)
    selectors = []
    for name in services:
        service = kubectl.get_service(namespace=namespace, name=name)
        if not service.spec.selector:
            raise RuntimeError(f"recommendation-path service has no pod selector: {name}")
        selectors.append(",".join(f"{key}={value}" for key, value in service.spec.selector.items()))
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        draining = []
        for selector in selectors:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("previous recommendation-path pods did not drain before the deadline")
            pods = kubectl.core_v1_api.list_namespaced_pod(
                namespace=namespace, label_selector=selector, _request_timeout=min(10.0, remaining)
            )
            draining.extend(pod.metadata.name for pod in pods.items if pod.metadata.deletion_timestamp is not None)
        if not draining:
            return
        time.sleep(2.0)
    raise RuntimeError(f"previous recommendation-path pods did not drain: {', '.join(draining)}")


class ThunderingHerdCascadeAstronomyShop(Problem):
    """Overlay recommendation so each ListRecommendations fans out ListProducts."""

    run_default_workload = False
    recommendation_deployment = "recommendation"
    source_path = _SOURCE_PATH
    configmap_name = _SOURCE_CONFIGMAP
    cache_flag = "recommendationCacheFailure"

    def __init__(self):
        super().__init__(app=_RecommendationSourceApp(load_generator_enabled=False))
        self.kubectl = KubeCtl()
        self.workload = RecommendationHerdWorkload(
            self.namespace,
            frontend_service=self.app.frontend_service,
            frontend_port=self.app.frontend_port,
        )
        self._injection_attempted = False
        self._replacement_content = _ASSET.read_text(encoding="utf-8")
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.recommendation_deployment}",
            namespace=self.namespace,
            description=ROOT_CAUSE_DESCRIPTION,
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ThunderingHerdMitigationOracle(problem=self)

    def _overlay(self, injector: ApplicationFaultInjector) -> None:
        injector.inject_source_file_override(
            deployment_name=self.recommendation_deployment,
            source_path=self.source_path,
            replacement_content=self._replacement_content,
            configmap_name=self.configmap_name,
            container_name=self.recommendation_deployment,
        )

    def _unoverlay(self, injector: ApplicationFaultInjector) -> None:
        injector.recover_source_file_override(
            deployment_name=self.recommendation_deployment,
            source_path=self.source_path,
            configmap_name=self.configmap_name,
            container_name=self.recommendation_deployment,
        )

    def _assert_overlay_live(self) -> None:
        command = (
            f"kubectl exec -n {self.namespace} deploy/{self.recommendation_deployment} "
            f"-c {self.recommendation_deployment} -- sha256sum {self.source_path}"
        )
        try:
            output = self.kubectl.exec_command_checked(command)
        except RuntimeError as exc:
            raise RuntimeError(f"recommendation overlay is not live at {self.source_path}") from exc
        expected_digest = hashlib.sha256(self._replacement_content.encode("utf-8")).hexdigest()
        if not output.split() or output.split()[0] != expected_digest:
            raise RuntimeError(f"recommendation overlay is not live at {self.source_path}: {output!r}")

    def _wait_for_recommendation(self) -> None:
        _wait_for_services(self.kubectl, self.namespace, [self.recommendation_deployment, "product-catalog"])

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        if self.fault_injected or self._injection_attempted:
            raise RuntimeError("fault injection is already active")
        self._injection_attempted = True
        injector = ApplicationFaultInjector(namespace=self.namespace)
        try:
            self.app.set_flag(self.cache_flag, False)
            self._overlay(injector)
            self._wait_for_recommendation()
            self._assert_overlay_live()
            self.mitigation_oracle.assert_fault_present()
            self.start_workload()
        except Exception:
            self.workload.stop()
            try:
                self._unoverlay(injector)
            except Exception as cleanup_error:
                print(f"[Cleanup] Failed to remove the recommendation overlay: {cleanup_error}")
            raise
        print(f"Service: {self.recommendation_deployment} | Namespace: {self.namespace}")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.workload.stop()
        injector = ApplicationFaultInjector(namespace=self.namespace)
        self._unoverlay(injector)
        self._wait_for_recommendation()

    def start_workload(self):
        self.workload.start_background(
            concurrency=self.mitigation_oracle.visible_concurrency,
            product_ids=self.mitigation_oracle.seed_product_ids,
        )

    def stop_workload(self):
        self.workload.stop()
