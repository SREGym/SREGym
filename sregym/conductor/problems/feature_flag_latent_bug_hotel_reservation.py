"""Feature flag latent bug - config change activates dormant error path in frontend.

Inspired by the Fastly June 8, 2021 outage: a configuration change activates a
latent bug that only manifests under specific circumstances (here, incoming
requests). The frontend runs a custom image with a dormant code path that, when
SEARCH_BACKEND_VERSION is true, returns HTTP 500 errors on every
search request. With the flag off the service behaves normally. With the flag on
AND requests arriving, the service returns errors to users while all pods remain
Running — no crash, no CrashLoopBackOff. The fix is to revert the flag and
restore the original frontend image.
"""

from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.feature_flag_http_probe_mitigation import FeatureFlagHttpProbeMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class FeatureFlagLatentBugHotelReservation(Problem):
    def __init__(self):
        self.faulty_service = "frontend"
        self.configmap_name = "frontend-runtime-config"
        self.flag_key = "SEARCH_BACKEND_VERSION"

        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"A configuration change set `{self.flag_key}` to true in ConfigMap "
                f"`{self.configmap_name}`, activating a dormant code path compiled into "
                "the frontend image. When that path is active and requests arrive, the "
                "frontend returns HTTP 500 errors on every hotel search request. All pods "
                "remain Running throughout — the failure is visible only at the service "
                "level as elevated error rates on the /hotels endpoint. The fix is to "
                f"revert `{self.flag_key}` to false in the ConfigMap and restore the "
                "original frontend image so the dormant path is no longer activated."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = CompoundedOracle(
            self,
            MitigationOracle(problem=self),
            FeatureFlagHttpProbeMitigationOracle(problem=self),
        )

    def _get_original_image(self) -> str:
        """Return the original frontend image — captured at inject time if available,
        otherwise read from the live deployment as fallback."""
        if hasattr(self, "original_image"):
            return self.original_image
        # Fallback: read from live deployment (works if recovery runs without prior injection)
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        for container in deployment.spec.template.spec.containers:
            if container.name == f"hotel-reserv-{self.faulty_service}":
                return container.image
        raise RuntimeError(f"Cannot determine original image for {self.faulty_service} — manual recovery required")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        # Capture original image before swapping so recovery restores the correct one
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        for container in deployment.spec.template.spec.containers:
            if container.name == f"hotel-reserv-{self.faulty_service}":
                self.original_image = container.image
                break
        injector.inject_feature_flag_experimental_routing(
            deployment_name=self.faulty_service,
            configmap_name=self.configmap_name,
            flag_key=self.flag_key,
        )
        self.app.start_workload()
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.recover_feature_flag_experimental_routing(
            deployment_name=self.faulty_service,
            configmap_name=self.configmap_name,
            flag_key=self.flag_key,
            original_image=getattr(self, "original_image", "yinfangchen/hotelreservation:latest"),
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
