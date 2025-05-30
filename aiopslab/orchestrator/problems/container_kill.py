"""Container kill problem in the HotelReservation application."""

from aiopslab.generators.fault.inject_symp import SymptomFaultInjector
from aiopslab.generators.workload.wrk import Wrk
from aiopslab.orchestrator.oracles.detection import DetectionOracle
from aiopslab.orchestrator.oracles.localization import LocalizationOracle
from aiopslab.orchestrator.oracles.mitigation import MitigationOracle
from aiopslab.orchestrator.problems.base import Problem
from aiopslab.paths import TARGET_MICROSERVICES
from aiopslab.service.apps.hotelres import HotelReservation
from aiopslab.service.kubectl import KubeCtl

from .helpers import get_frontend_url


class ChaosMeshContainerKill(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "geo"
        self.faulty_container = "hotel-reserv-geo"
        self.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )
        self.symptom_injector = SymptomFaultInjector(namespace=self.namespace)
        self.experiment_name = "container-kill-mesh"  # Hardcoding the known experiment name
        self.chaos_type = "podchaos"  # Hardcoding the type of chaos
        # === Attach evaluation oracles ===
        self.detection_oracle = DetectionOracle(problem=self, expected="Yes")

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

        self.mitigation_oracle = MitigationOracle(problem=self)

        # === Workload setup ===
        self.payload_script = TARGET_MICROSERVICES / "socialNetwork/wrk2/scripts/social-network/compose-post.lua"

    def start_workload(self):
        print("== Start Workload ==")
        frontend_url = get_frontend_url(self.app)

        wrk = Wrk(rate=100, dist="exp", connections=2, duration=10, threads=2)
        wrk.start_workload(
            payload_script=self.payload_script,
            url=f"{frontend_url}",
        )

    def inject_fault(self):
        print("== Fault Injection ==")
        self.symptom_injector.inject_container_kill(self.faulty_service, self.faulty_container)
        print(f"Service: {self.faulty_service} | Container: {self.faulty_container} | Namespace: {self.namespace}\n")

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.symptom_injector.recover_container_kill()
        print(f"Recovered Service: {self.faulty_service} | Namespace: {self.namespace}\n")
