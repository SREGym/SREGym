"""No operation problem for HotelReservation or SocialNetwork applications to test false positive."""

from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_noop import NoopFaultInjector
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.apps.hotelres import HotelReservation
from srearena.service.apps.socialnet import SocialNetwork
from srearena.service.kubectl import KubeCtl


class NoOp(Problem):
    def __init__(self, app_name: str = "hotel_reservation"):
        self.app_name = app_name

        if self.app_name == "hotel_reservation":
            self.app = HotelReservation()
        elif self.app_name == "social_network":
            self.app = SocialNetwork()
        elif self.app_name == "astronomy_shop":
            self.app = AstronomyShop()
        else:
            raise ValueError(f"Unsupported app_name: {app_name}")

        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = None
        self.injector = NoopFaultInjector(namespace=self.namespace)
        # === Attach evaluation oracles ===
        self.detection_oracle = DetectionOracle(problem=self, expected="No")

    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector._inject(fault_type="no_op", microservices=[self.faulty_service], duration="200s")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector._recover(
            fault_type="no_op",
        )
