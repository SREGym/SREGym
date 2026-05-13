"""Catastrophic regex backtracking (ReDoS) — code-level fault.

Derived from the `catastrophic-regex-backtracking` template mined from public
postmortems (classic ReDoS: Cloudflare 2019, Stack Overflow, etc.). Unlike the
config-level faults in this repo, the fix here is a real source-code change:
the agent has to patch the product-reviews service's `database.py` to remove
or repair a validation regex with nested quantifiers.

The fault is injected by overlaying `/app/database.py` inside the running
product-reviews container with a patched version via a ConfigMap subPath
mount. The overlay disappears on recovery.
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

# (pod_path, asset_filename, workspace_path) per supported variant. The
# workspace_path lines up with the vendored source tree in
# SREGym-applications/astronomy-shop-src/ so the agent sees a real directory
# layout of the upstream service.
_VARIANTS = {
    ("product-reviews", "database"): (
        "/app/database.py",
        "catastrophic_regex_database.py",
        "src/product-reviews/database.py",
    ),
    ("recommendation", "server"): (
        "/app/recommendation_server.py",
        "catastrophic_regex_recommendation.py",
        "src/recommendation/recommendation_server.py",
    ),
}


class CatastrophicRegexBacktracking(Problem):
    def __init__(
        self,
        app_name: str = "astronomy_shop",
        faulty_service: str = "product-reviews",
        target_file: str = "database",
    ):
        self.app_name = app_name
        self.faulty_service = faulty_service
        self.target_file = target_file

        if self.app_name != "astronomy_shop":
            raise ValueError(
                f"CatastrophicRegexBacktracking only supports astronomy_shop, got {app_name}"
            )
        variant_key = (self.faulty_service, self.target_file)
        if variant_key not in _VARIANTS:
            raise ValueError(
                f"CatastrophicRegexBacktracking has no variant for {variant_key}; "
                f"known variants: {sorted(_VARIANTS)}"
            )

        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        source_path, asset_name, workspace_path = _VARIANTS[variant_key]
        self.source_path = source_path
        self.configmap_name = f"{self.faulty_service}-{self.target_file}-src-override"
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
                "A recent patch to the product-reviews service added an input-"
                "validation regex to /app/database.py that has nested quantifiers: "
                "`^(([A-Z0-9]+)+[a-z])*$`. The validator also left-pads the input "
                "with 16 'X' characters before matching, producing a long input that "
                "never satisfies the required trailing `[a-z]`. Every review lookup "
                "forces the regex engine into catastrophic backtracking, pinning the "
                "product-reviews pod's CPU and causing request latency to spike into "
                "the seconds. The fix is a source-code change: remove the ReDoS "
                "pattern (or replace with a linear-time validator) in database.py."
            ),
        )

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        # Behavioral mitigation oracle: probe the actual user-visible code
        # path on the affected service. The product-reviews/database variant
        # probes the DB-fetch function; the product-reviews/server and
        # recommendation/server variants probe the gRPC handler.
        if self.faulty_service == "product-reviews" and self.target_file == "database":
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
