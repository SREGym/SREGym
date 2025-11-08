from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.conductor.oracles.memoryquota_itself_localization_oracle import MemoryQuotaItselfLocalizationOracle
from sregym.conductor.oracles.namespace_memory_limit_mitigation import NamespaceMemoryLimitMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class NamespaceMemoryLimit(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "search"
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        super().__init__(app=self.app, namespace=self.namespace)

        self.localization_oracle = MemoryQuotaItselfLocalizationOracle(
            problem=self, namespace=self.namespace, expected_memoryquota_name=self.faulty_service
        )
        self.mitigation_oracle = NamespaceMemoryLimitMitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_namespace_memory_limit(
            deployment_name=self.faulty_service, namespace=self.namespace, memory_limit="1Gi"
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_namespace_memory_limit(deployment_name=self.faulty_service, namespace=self.namespace)
