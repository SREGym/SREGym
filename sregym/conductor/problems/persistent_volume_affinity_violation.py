from sregym.conductor.oracles.deployment_itself_localization_oracle import DeploymentItselfLocalizationOracle
from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.or_localization_oracle import OrLocalizationOracle
from sregym.conductor.oracles.pv_itself_localization_oracle import PVItselfLocalizationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class PersistentVolumeAffinityViolation(Problem):
    def __init__(self, app_name: str = "Social Network", faulty_service: str = "user-service"):
        self.apps = AppRegistry()
        self.app = self.apps.get_app_instance(app_name)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        super().__init__(app=self.app, namespace=self.app.namespace)

        # === Attach evaluation oracles ===
        oracle1 = DeploymentItselfLocalizationOracle(
            problem=self, namespace=self.namespace, expected_deployment_names=[self.faulty_service]
        )
        oracle2 = PVItselfLocalizationOracle(problem=self, namespace=self.namespace, expected_pv_name=f"temp-pv")
        # claim RC in any of it is ok
        self.localization_oracle = OrLocalizationOracle(
            problem=self, namespace=self.namespace, oracle1=oracle1, oracle2=oracle2
        )

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
