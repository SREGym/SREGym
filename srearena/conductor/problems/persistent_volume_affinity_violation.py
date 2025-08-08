from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected
from srearena.service.apps.registry import AppRegistry

class PersistentVolumeAffinityViolation(Problem):
    def __init__(self, app_name: str = "Social Network", faulty_service: str = "user-service"):
        self.apps = AppRegistry()
        self.app = self.apps.get_app_instance(app_name)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        super().__init__(app=self.app, namespace=self.app.namespace)

        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])
        self.mitigation_oracle = MitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        print("Injecting persistent volume affinity violation...")
        
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="persistent_volume_affinity_violation",
            microservices=[self.faulty_service],
        )

        print(f"Expected effect: {self.faulty_service} pod should be stuck in Pending state")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="persistent_volume_affinity_violation",
            microservices=[self.faulty_service],
        )

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
