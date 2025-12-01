from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
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
        self.root_cause = f"The namespace has a ResourceQuota with a memory limit (1Gi) that is too restrictive, preventing the deployment `{self.faulty_service}` from scheduling new pods or causing existing pods to be evicted."
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
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
