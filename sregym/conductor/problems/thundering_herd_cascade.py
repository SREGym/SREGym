"""Duplicated in-flight ListProducts calls from Astronomy Shop recommendation.

Calibrated and supported on x86-64 only.
"""

from __future__ import annotations

from pathlib import Path

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.thundering_herd_mitigation import ThunderingHerdMitigationOracle
from sregym.conductor.problems.base import EditableFile, Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.workload.recommendation_herd import RecommendationHerdWorkload
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


_ASSET = Path(__file__).parent / "assets" / "thundering_herd_cascade_recommendation.py"

ROOT_CAUSE_DESCRIPTION = (
    "The recommendation service issues about ten equivalent ListProducts RPCs "
    "for each ListRecommendations with no single-flight or coalescing. Concurrent "
    "clients therefore multiply catalog load; this is duplicated in-flight work, "
    "not a retry storm. Pods stay Running. A diagnosis that blames product-catalog "
    "alone is incomplete: the catalog is healthy and the caller is multiplying work. "
    "Calibrated and supported on x86-64 only."
)


class ThunderingHerdCascadeAstronomyShop(Problem):
    """Overlay recommendation so each ListRecommendations fans out ListProducts."""

    run_default_workload = False
    recommendation_deployment = "recommendation"
    source_path = "/app/recommendation_server.py"
    configmap_name = "recommendation-src-override"
    cache_flag = "recommendationCacheFailure"
    overlay_command = (
        "/venv/bin/opentelemetry-instrument",
        "/venv/bin/python",
        "/app/recommendation_server.py",
    )

    def __init__(self):
        super().__init__(app=AstronomyShop())
        self.kubectl = KubeCtl()
        self.workload = RecommendationHerdWorkload(
            self.namespace,
            frontend_service=self.app.frontend_service,
            frontend_port=self.app.frontend_port,
        )
        self._injection_attempted = False
        self._replacement_content = _ASSET.read_text(encoding="utf-8")
        self.editable_files = [
            EditableFile(
                workspace_path="recommendation_server.py",
                pod_path=self.source_path,
                deployment=self.recommendation_deployment,
                configmap_name=self.configmap_name,
                host_source=str(_ASSET),
            )
        ]
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
            command=list(self.overlay_command),
        )

    def _unoverlay(self, injector: ApplicationFaultInjector) -> None:
        injector.recover_source_file_override(
            deployment_name=self.recommendation_deployment,
            source_path=self.source_path,
            configmap_name=self.configmap_name,
            container_name=self.recommendation_deployment,
        )

    def _assert_overlay_live(self) -> None:
        marker = "for _ in range(10)"
        command = (
            f"kubectl exec -n {self.namespace} deploy/{self.recommendation_deployment} "
            f"-c {self.recommendation_deployment} -- grep -F '{marker}' {self.source_path}"
        )
        try:
            output = self.kubectl.exec_command_checked(command)
        except RuntimeError as exc:
            raise RuntimeError(
                f"recommendation overlay is not live at {self.source_path}"
            ) from exc
        if marker not in output:
            raise RuntimeError(
                f"recommendation overlay is not live at {self.source_path}: {output!r}"
            )

    def _wait_for_recommendation(self) -> None:
        self.kubectl.wait_for_ready(
            self.namespace,
            service_names=[self.recommendation_deployment, "product-catalog"],
        )

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
        except Exception:
            try:
                self._unoverlay(injector)
            except Exception as cleanup_error:
                print(f"[Cleanup] Failed to remove the recommendation overlay: {cleanup_error}")
            self.workload.stop()
            raise
        print(f"Service: {self.recommendation_deployment} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        try:
            injector = ApplicationFaultInjector(namespace=self.namespace)
            self._unoverlay(injector)
            self._wait_for_recommendation()
        finally:
            self.workload.stop()

    def stop_workload(self):
        self.workload.stop()
