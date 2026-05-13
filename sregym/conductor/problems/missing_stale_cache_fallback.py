"""Missing stale-cache fallback on a hard upstream call.

Derived from the `missing-stale-cache-fallback` template mined from public
postmortems. Common production bug shape: service X fetches from upstream Y
with no graceful-degradation path — no cache, no fallback, no retry budget.
Under normal load X works; the first transient Y slowdown takes X down with
it. The fix is always a code change: wrap the upstream call in try/except and
return stale / empty / cached data on error.
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


# (pod_path, asset_filename, workspace_path) per supported variant.
_VARIANTS = {
    "recommendation": (
        "/app/recommendation_server.py",
        "missing_stale_cache_fallback_recommendation.py",
        "src/recommendation/recommendation_server.py",
    ),
    "product-reviews": (
        "/app/database.py",
        "missing_stale_cache_fallback_product_reviews.py",
        "src/product-reviews/database.py",
    ),
}


class MissingStaleCacheFallback(Problem):
    def __init__(
        self,
        app_name: str = "astronomy_shop",
        faulty_service: str = "recommendation",
    ):
        self.app_name = app_name
        self.faulty_service = faulty_service

        if self.app_name != "astronomy_shop":
            raise ValueError(
                f"MissingStaleCacheFallback only supports astronomy_shop, got {app_name}"
            )
        if self.faulty_service not in _VARIANTS:
            raise ValueError(
                f"MissingStaleCacheFallback has no variant for service '{faulty_service}'; "
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
                f"The {self.faulty_service} service's upstream call has been "
                "pinned to a 1 ms timeout with no error handling and no cache "
                "fallback. Every request now fails because the upstream cannot "
                "respond in under a millisecond across the pod network. The "
                f"fix is a source-code change in {self.source_path}: wrap the "
                "call in try/except and fall back to an empty list or cached "
                "data on error (or restore a reasonable timeout)."
            ),
        )

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        if self.faulty_service == "product-reviews":
            # Probe the DB-fetch path directly via a SELECT-style oracle.
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
