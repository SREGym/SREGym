"""Wrong binary usage problem in the HotelReservation application."""

from aiopslab.generators.fault.inject_virtual import VirtualizationFaultInjector
from aiopslab.generators.workload.wrk import Wrk
from aiopslab.orchestrator.oracles.detection import DetectionOracle
from aiopslab.orchestrator.oracles.localization import LocalizationOracle
from aiopslab.orchestrator.oracles.mitigation import MitigationOracle
from aiopslab.orchestrator.problems.base import Problem
from aiopslab.paths import TARGET_MICROSERVICES
from aiopslab.service.apps.hotelres import HotelReservation
from aiopslab.service.kubectl import KubeCtl

from .helpers import get_frontend_url


class WrongBinUsage(Problem):
    def __init__(self, faulty_service: str = "profile"):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service

        self.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )
        # === Attach evaluation oracles ===
        self.detection_oracle = DetectionOracle(problem=self, expected="Yes")

        self.localization_oracle = LocalizationOracle(problem=self, expected=[faulty_service])

        self.mitigation_oracle = MitigationOracle(problem=self)

    def start_workload(self):
        print("== Start Workload ==")
        frontend_url = get_frontend_url(self.app)

        wrk = Wrk(rate=10, dist="exp", connections=2, duration=10, threads=2)
        wrk.start_workload(
            payload_script=self.payload_script,
            url=f"{frontend_url}",
        )

    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="wrong_bin_usage",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="wrong_bin_usage",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
