"""No operation problem for HotelReservation or SocialNetwork applications to test false positive."""

from typing import Any

from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_noop import NoopFaultInjector
from srearena.generators.workload.wrk import Wrk
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.apps.hotelres import HotelReservation
from srearena.service.apps.socialnet import SocialNetwork
from srearena.service.kubectl import KubeCtl

from .helpers import get_frontend_url


class NoOp(Problem):
    def __init__(self, app_name: str = "hotel"):
        self.app_name = app_name

        if self.app_name == "hotel_reservation":
            self.app = HotelReservation()
            self.payload_script = (
                TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
            )
        elif self.app_name == "social_network":
            self.app = SocialNetwork()
            self.payload_script = TARGET_MICROSERVICES / "socialNetwork/wrk2/scripts/social-network/compose-post.lua"
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

    def start_workload(self):
        if self.app_name != "astronomy_shop":
            print("== Start Workload ==")
            frontend_url = get_frontend_url(self.app)

            wrk = Wrk(rate=10, dist="exp", connections=2, duration=10, threads=2)
            wrk.start_workload(
                payload_script=self.payload_script,
                url=f"{frontend_url}",
            )
        else:
            # Skip workload since astronomy shop has its own workload generator
            print("== Workload Skipped ==")

    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector._inject(fault_type="no_op", microservices=[self.faulty_service], duration="200s")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector._recover(
            fault_type="no_op",
        )
