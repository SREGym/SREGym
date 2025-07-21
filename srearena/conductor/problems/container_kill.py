"""Container kill problem in the HotelReservation application."""

from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_symp import SymptomFaultInjector
from srearena.service.apps.hotelres import HotelReservation
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class ChaosMeshContainerKill(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "geo"
        self.faulty_container = "hotel-reserv-geo"
        self.symptom_injector = SymptomFaultInjector(namespace=self.namespace)
        self.experiment_name = "container-kill-mesh"  # Hardcoding the known experiment name
        self.chaos_type = "podchaos"  # Hardcoding the type of chaos
        super().__init__(app=self.app, namespace=self.app.namespace)
        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.symptom_injector.inject_container_kill(self.faulty_service, self.faulty_container)
        print(f"Service: {self.faulty_service} | Container: {self.faulty_container} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.symptom_injector.recover_container_kill()
        print(f"Recovered Service: {self.faulty_service} | Namespace: {self.namespace}\n")
