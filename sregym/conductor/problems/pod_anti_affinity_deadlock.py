"""Pod Anti-Affinity Deadlock problem for microservice applications."""

import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class PodAntiAffinityDeadlock(Problem):
    def __init__(self, faulty_service: str = "user-service"):
        self.app = SocialNetwork()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self.root_cause = f"The deployment `{self.faulty_service}` has strict pod anti-affinity rules (requiredDuringSchedulingIgnoredDuringExecution) that prevent multiple replicas from being scheduled on the same node, but with insufficient nodes, causing a scheduling deadlock where pods remain in Pending state."
        super().__init__(app=self.app, namespace=self.app.namespace)

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        # Create workload for evaluation
        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        print("Creating Pod Anti-Affinity Deadlock...")
        print("Setting requiredDuringScheduling anti-affinity that excludes all nodes")

        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="pod_anti_affinity_deadlock",
            microservices=[self.faulty_service],
        )

        # Wait for the deadlock to manifest
        time.sleep(30)

        print(f"Expected effect: Pods should be in Pending state with:")
        print(f"  '0/X nodes are available: X node(s) didn't match pod anti-affinity rules'")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        print("Removing pod anti-affinity deadlock...")
        print("Changing requiredDuring to preferredDuring or removing anti-affinity rules")

        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="pod_anti_affinity_deadlock",
            microservices=[self.faulty_service],
        )

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
