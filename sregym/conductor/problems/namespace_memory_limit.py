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
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "search"
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"Namespace-level ResourceQuota memory limit (`1Gi`) is set too low for deployment `{self.faulty_service}`, "
                "so new pods cannot be admitted or scheduled when memory demand rises. Existing pods may also be evicted "
                "as aggregate namespace usage breaches quota constraints. Users observe unstable availability, failed scaling, "
                "and intermittent request failures during routine workload pressure."
            ),
        )

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
