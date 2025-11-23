from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.sustained_readiness import SustainedReadinessOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class LivenessProbeTooAggressive(Problem):
    def __init__(self, app_name: str = "social_network"):
        self.app_name = app_name
        self.faulty_service = "aux-service"

        if app_name == "social_network":
            self.app = SocialNetwork()
        elif app_name == "hotel_reservation":
            self.app = HotelReservation()
        elif app_name == "astronomy_shop":
            self.app = AstronomyShop()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.injector = VirtualizationFaultInjector(namespace=self.app.namespace)
        self.root_cause = f"The deployment `{self.faulty_service}` has an overly aggressive liveness probe (initialDelaySeconds=0, periodSeconds=1, failureThreshold=1) with terminationGracePeriodSeconds=0, causing pods to be killed immediately if the probe fails."
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = SustainedReadinessOracle(problem=self, sustained_period=30)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_liveness_probe_too_aggressive([self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.app.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_liveness_probe_too_aggressive([self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.app.namespace}\n")
