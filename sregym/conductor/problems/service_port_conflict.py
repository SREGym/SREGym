from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.apps.train_ticket import TrainTicket
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ServicePortConflict(Problem):
    """Problem that injects a hostPort conflict causing pods to get stuck in Pending state.

    This simulates issue where a service uses a hostPort that conflicts with
    another service in a different namespace (e.g., prometheus-node-exporter using port 9100).
    """

    def __init__(self, app_name: str = "astronomy_shop", faulty_service: str = "ad"):
        self.app_name = app_name
        self.faulty_service = faulty_service
        self.conflicting_port = 9100  # Conflicts with prometheus-node-exporter

        if app_name == "social_network":
            self.app = SocialNetwork()
        elif app_name == "hotel_reservation":
            self.app = HotelReservation()
        elif app_name == "astronomy_shop":
            self.app = AstronomyShop()
        elif app_name == "train_ticket":
            self.app = TrainTicket()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.root_cause = f"The deployment `{self.faulty_service}` is configured with hostPort {self.conflicting_port}, which conflicts with another service in a different namespace, causing pods to get stuck in Pending state with FailedScheduling error."
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_service_port_conflict(
            microservices=[self.faulty_service],
            conflicting_port=self.conflicting_port,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_service_port_conflict(
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
