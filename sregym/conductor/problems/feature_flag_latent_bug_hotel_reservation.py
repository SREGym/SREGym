"""Feature flag latent bug - config change activates dormant buggy branch in frontend."""

from sregym.conductor.oracles.feature_flag_mitigation import FeatureFlagMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class FeatureFlagLatentBugHotelReservation(Problem):
    def __init__(self):
        self.faulty_service = "frontend"
        self.configmap_name = "frontend-feature-flags"
        self.flag_key = "FEATURE_FLAG_EXPERIMENTAL_BRANCH"
        self.flag_safe_value = "false"

        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"ConfigMap `{self.configmap_name}` has `{self.flag_key}` set to `true`, "
                "activating a dormant buggy code branch in the frontend service. "
                "All replicas read the shared ConfigMap and crash on startup, "
                "causing the frontend deployment to enter CrashLoopBackOff across all pods. "
                "The fix is to set the flag to `false` in the ConfigMap and restart the deployment."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = FeatureFlagMitigationOracle(
            problem=self,
            configmap_name=self.configmap_name,
            flag_key=self.flag_key,
            flag_safe_value=self.flag_safe_value,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.inject_feature_flag_latent_bug(
            deployment_name=self.faulty_service,
            configmap_name=self.configmap_name,
            flag_key=self.flag_key,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.recover_feature_flag_latent_bug(
            deployment_name=self.faulty_service,
            configmap_name=self.configmap_name,
            flag_key=self.flag_key,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
