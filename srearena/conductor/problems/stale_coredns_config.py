from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.dns_resolution_mitigation import DNSResolutionMitigationOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.workload import WorkloadOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.apps.social_network import SocialNetwork
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


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

        self.localization_oracle = LocalizationOracle(problem=self, expected=["coredns"])

        self.app.create_workload()
        self.mitigation_oracle = CompoundedOracle(
            self,
            DNSResolutionMitigationOracle(problem=self),
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
        )

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
