from sregym.conductor.oracles.dns_resolution_mitigation import DNSResolutionMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class StaleCoreDNSConfig(Problem):
    def __init__(self, app_name="astronomy_shop"):
        self.app_name = app_name
        self.faulty_service = None

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
        self.root_cause = "CoreDNS is configured with a stale NXDOMAIN template for all .svc.cluster.local domains, causing DNS resolution to fail for all cluster-internal services."

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = DNSResolutionMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        self.injector._inject(
            fault_type="stale_coredns_config",
            microservices=None,
        )
        print(f"Injected stale CoreDNS config | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        self.injector._recover(
            fault_type="stale_coredns_config",
            microservices=None,
        )
        print(f"Recovered from stale CoreDNS config | Namespace: {self.namespace}\n")
