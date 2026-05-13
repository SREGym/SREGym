"""Latent recovery-triggered cascading failure.

Derived from the `latent-recovery-triggered-cascading-failure` template mined
from public postmortems. A long-running service accumulates cache / connection
state; its code contains an "innocent" expensive warm-up path that runs on
every cold start but is never exercised in steady-state testing. Months later
something benign — a deploy, an OOM kill, a node reboot — forces a restart,
and the sudden sync call to upstream creates a load spike that takes the
cluster down just as recovery is supposed to begin.
"""

from pathlib import Path

from sregym.conductor.oracles.behavioral_probes import RolloutLatencyOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
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
        "latent_recovery_triggered_recommendation.py",
        "src/recommendation/recommendation_server.py",
    ),
    "product-reviews": (
        "/app/product_reviews_server.py",
        "latent_recovery_triggered_product_reviews_server.py",
        "src/product-reviews/product_reviews_server.py",
    ),
}


class LatentRecoveryTriggeredCascadingFailure(Problem):
    def __init__(
        self,
        app_name: str = "astronomy_shop",
        faulty_service: str = "recommendation",
    ):
        self.app_name = app_name
        self.faulty_service = faulty_service
        if self.app_name != "astronomy_shop":
            raise ValueError(
                f"LatentRecoveryTriggeredCascadingFailure only supports astronomy_shop, got {app_name}"
            )
        if self.faulty_service not in _VARIANTS:
            raise ValueError(
                f"LatentRecoveryTriggeredCascadingFailure has no variant for service '{faulty_service}'; "
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
                f"The {self.faulty_service} service's {self.source_path} has "
                "been changed to run 50 serial product-catalog ListProducts "
                "calls between creating the gRPC server and calling "
                "server.start(). Under steady-state this startup warm-up is "
                "invisible, but every pod restart now blocks for seconds "
                "before the pod becomes Ready, and produces a concurrent load "
                "spike on product-catalog just when recovery is supposed to "
                "begin — a classic latent recovery-triggered cascading "
                "failure. The fix is a source-level change: remove the serial "
                "warm-up, defer it to lazy initialization on first request, "
                "or bound it to a small number of calls with backoff."
            ),
        )

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = RolloutLatencyOracle(problem=self)

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
