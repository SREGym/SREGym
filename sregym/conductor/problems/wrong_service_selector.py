from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class WrongServiceSelector(Problem):
    def __init__(self, app_name="astronomy_shop", faulty_service="frontend"):
        self.app_name = app_name
        self.faulty_service = faulty_service

        if app_name == "social_network":
            self.app = SocialNetwork()
        elif app_name == "hotel_reservation":
            self.app = HotelReservation()
        elif app_name == "astronomy_shop":
            self.app = AstronomyShop()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The service `{self.faulty_service}` has a misconfigured selector that adds an incorrect label, "
                "so it no longer matches the intended backing pods. The service has zero or insufficient endpoints "
                "despite healthy-looking deployments, causing routing failures at the service layer. Users observe "
                "connection resets/timeouts and partial outages when requests are sent through this service."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = ServiceEndpointMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="wrong_service_selector",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="wrong_service_selector",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
