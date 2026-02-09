from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.resource_quota_exhaustion_mitigation import ResourceQuotaExhaustionMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ResourceQuotaExhaustion(Problem):
    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "frontend"
        self.quota_name = "team-quota"
        self.target_replicas = 5
        self.original_replicas = None
        self.current_pod_count = None
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)

        self.root_cause = (
            "The namespace has a ResourceQuota limiting pod count that is already consumed by existing workloads. "
            f"When attempting to scale the deployment {self.faulty_service}, new pods remain in Pending state "
            "because the quota for pod count is exhausted."
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ResourceQuotaExhaustionMitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.current_pod_count, self.original_replicas = self.injector.inject_resource_quota_exhaustion(
            deployment_name=self.faulty_service,
            namespace=self.namespace,
            quota_name=self.quota_name,
            target_replicas=self.target_replicas,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_resource_quota_exhaustion(
            deployment_name=self.faulty_service,
            namespace=self.namespace,
            quota_name=self.quota_name,
            original_replicas=self.original_replicas,
        )
