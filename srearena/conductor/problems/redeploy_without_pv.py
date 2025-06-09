"""Redeployment of the HotelReservation application but do not handle PV."""

from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.oracles.workload import WorkloadOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.hotelres import HotelReservation
from srearena.service.kubectl import KubeCtl


class RedeployWithoutPV(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.app.payload_script = (
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

        self.app.create_workload()
        self.mitigation_oracle = CompoundedOracle(
            self,
            MitigationOracle(problem=self),
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
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
