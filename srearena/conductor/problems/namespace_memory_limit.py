from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.namespace_memory_limit_mitigation import NamespaceMemoryLimitMitigationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class NamespaceMemoryLimit(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "search"
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        super().__init__(app=self.app, namespace=self.namespace)

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])
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
