"""Redeployment of the HotelReservation application but do not handle PV."""

from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.generators.workload.wrk import Wrk
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.hotelres import HotelReservation
from srearena.service.kubectl import KubeCtl

from .helpers import get_frontend_url


class RedeployWithoutPV(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )
        self.faulty_service = [
            "geo",
            "profile",
            "rate",
            "recommendation",
            "reservation",
            "mongodb-geo",
            "mongodb-profile",
            "mongodb-rate",
            "mongodb-recommendation",
            "mongodb-reservation",
            "mongodb-user",
        ]
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        # === Attach evaluation oracles ===
        self.detection_oracle = DetectionOracle(problem=self, expected="Yes")

        self.localization_oracle = LocalizationOracle(problem=self, expected=self.faulty_service)

        self.mitigation_oracle = MitigationOracle(problem=self)

    def start_workload(self):
        print("== Start Workload ==")
        frontend_url = get_frontend_url(self.app)

        wrk = Wrk(rate=10, dist="exp", connections=2, duration=5, threads=2)
        wrk.start_workload(
            payload_script=self.payload_script,
            url=f"{frontend_url}",
        )

    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_redeploy_without_pv(app=self.app)
        # self.injector._inject(
        #     fault_type="redepoly_without_pv",
        #     app=self.app,
        # )
        # print(f"Application: {self.faulty_service} | Namespace: {self.namespace}\n")

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_redeploy_without_pv(app=self.app)
        # self.injector._recover(
        #     fault_type="redepoly_without_pv",
        #     app=self.app,
        # )
        # print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
