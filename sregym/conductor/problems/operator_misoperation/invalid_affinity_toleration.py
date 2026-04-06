"""
This misoperation specifies an invalid toleration effect.
"""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.operator_misoperation.invalid_affinity_mitigation import InvalidAffinityMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_operator import K8SOperatorFaultInjector
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class K8SOperatorInvalidAffinityTolerationFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        self.app = FleetCast()
        self.namespace = self.app.namespace
        print("App's namespace:", self.namespace)
        super().__init__(app=self.app, namespace="tidb-cluster")
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component="customresource/tidbcluster/basic",
            namespace="tidb-cluster",
            description=(
                "The TiDBCluster spec contains an invalid toleration effect value, so scheduler constraints cannot be "
                "satisfied and TiDB pods remain Pending with unschedulable events."
            ),
        )
        self.app.create_workload()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.mitigation_oracle = InvalidAffinityMitigationOracle(problem=self, deployment_name="basic")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = K8SOperatorFaultInjector(namespace="tidb-cluster")
        injector.inject_invalid_affinity_toleration()
        print(f"[FAULT INJECTED] {self.faulty_service} invalid affinity toleration failure\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        injector = K8SOperatorFaultInjector(namespace="tidb-cluster")
        injector.recover_invalid_affinity_toleration()
        print(f"[FAULT INJECTED] {self.faulty_service} invalid affinity toleration failure\n")
