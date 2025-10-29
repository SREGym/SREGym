from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class LivenessProbeMisconfiguration(Problem):
    def __init__(self, app_name="social_network", faulty_service="user-service"):
        self.app_name = app_name
        self.faulty_service = faulty_service

        if app_name == "social_network":
            self.app = SocialNetwork()
            self.app.create_workload(duration=30)

        elif app_name == "hotel_reservation":
            self.app = HotelReservation()
            self.app.create_workload(duration=30)

        elif app_name == "astronomy_shop":
            self.app = AstronomyShop()
            self.app.create_workload()

        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.injector = VirtualizationFaultInjector(namespace=self.app.namespace)

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector._inject(
            fault_type="liveness_probe_misconfiguration",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector._recover(
            fault_type="liveness_probe_misconfiguration",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
