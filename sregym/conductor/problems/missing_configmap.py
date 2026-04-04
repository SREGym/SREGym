from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class MissingConfigMap(Problem):
    def __init__(self, app_name: str = "social_network", faulty_service: str = "media-mongodb"):
        self.faulty_service = faulty_service
        self.app_name = app_name

        if self.app_name == "social_network":
            self.app = SocialNetwork()
        elif self.app_name == "hotel_reservation":
            self.app = HotelReservation()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"A required ConfigMap for deployment `{self.faulty_service}` has been deleted, so pods lose required "
                "runtime configuration during startup and reload. Affected pods fail to initialize correctly or run "
                "with invalid defaults, leading to NotReady/CrashLoop behavior and unstable service operation. "
                "Users observe request failures and degraded functionality for features backed by this component."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(fault_type="missing_configmap", microservices=[self.faulty_service])

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(fault_type="missing_configmap", microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
