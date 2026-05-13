"""Thundering-herd cascade from a missing single-flight pattern.

Derived from the `thundering-herd-cascade` template mined from public
postmortems. A canonical cascading-failure trigger: a service has no request-
coalescing around an expensive upstream call, so N concurrent clients produce
N upstream requests — and under load that multiplier is enough to take the
upstream down. With the upstream degraded, the local service's retries spike
further, and the cascade propagates.
"""

from pathlib import Path

from sregym.conductor.oracles.behavioral_probes import RecommendationLatencyOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.redos_behavioral import ReDoSBehavioralOracle
from sregym.conductor.problems.base import EditableFile, Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


_ASSETS_DIR = Path(__file__).parent / "assets"
_VENDORED_ROOT = (
    Path(__file__).resolve().parents[3]
    / "SREGym-applications"
    / "astronomy-shop-src"
)


_VARIANTS = {
    "recommendation": (
        "/app/recommendation_server.py",
        "thundering_herd_cascade_recommendation.py",
        "src/recommendation/recommendation_server.py",
    ),
    "product-reviews": (
        "/app/database.py",
        "thundering_herd_cascade_product_reviews.py",
        "src/product-reviews/database.py",
    ),
}


class ThunderingHerdCascade(Problem):
    def __init__(
        self,
        app_name: str = "astronomy_shop",
        faulty_service: str = "recommendation",
    ):
        self.app_name = app_name
        self.faulty_service = faulty_service
        if self.app_name != "astronomy_shop":
            raise ValueError(
                f"ThunderingHerdCascade only supports astronomy_shop, got {app_name}"
            )
        if self.faulty_service not in _VARIANTS:
            raise ValueError(
                f"ThunderingHerdCascade has no variant for service '{faulty_service}'; "
                f"known variants: {sorted(_VARIANTS)}"
            )

        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        source_path, asset_name, workspace_path = _VARIANTS[self.faulty_service]
        self.source_path = source_path
        self.configmap_name = f"{self.faulty_service}-src-override"
        self._replacement_content = (_ASSETS_DIR / asset_name).read_text()

        self.vendored_source_root = _VENDORED_ROOT
        self.editable_files = [
            EditableFile(
                workspace_path=workspace_path,
                pod_path=self.source_path,
                deployment=self.faulty_service,
                configmap_name=self.configmap_name,
            ),
        ]

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.faulty_service} service's upstream/DB call fires "
                "multiple back-to-back requests per incoming call with no "
                "caching, no request coalescing, and no deduplication. Under "
                "concurrency the upstream sees an N× load multiplier, "
                "saturates, and takes the caller down with it. Fix is a "
                "source-level change: implement single-flight or pooling so "
                "concurrent callers share one in-flight request."
            ),
        )

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        if self.faulty_service == "product-reviews":
            self.mitigation_oracle = ReDoSBehavioralOracle(problem=self)
        else:
            self.mitigation_oracle = RecommendationLatencyOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.inject_source_file_override(
            deployment_name=self.faulty_service,
            source_path=self.source_path,
            replacement_content=self._replacement_content,
            configmap_name=self.configmap_name,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.recover_source_file_override(
            deployment_name=self.faulty_service,
            source_path=self.source_path,
            configmap_name=self.configmap_name,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
