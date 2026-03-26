"""
This fault specifies a non-existent storage class.
"""

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.operator_misoperation.non_existent_storage_mitigation import (
    NonExistentStorageClassMitigationOracle,
)
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_operator import K8SOperatorFaultInjector
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class K8SOperatorNonExistentStorageFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        self.app = FleetCast()
        super().__init__(app=self.app, namespace="tidb-cluster")
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.problem_id = "operator_non_existent_storage"
        self.root_cause = "The TiDBCluster custom resource specifies a non-existent StorageClass, causing PVC creation to fail and pods to remain in Pending state."
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.resolution_oracle = NonExistentStorageClassMitigationOracle(problem=self, deployment_name="basic")
        self.mitigation_oracle = AlertOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = K8SOperatorFaultInjector(namespace="tidb-cluster")
        injector.inject_non_existent_storage()
        print(f"[FAULT INJECTED] {self.faulty_service} non-existent storage failure\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = K8SOperatorFaultInjector(namespace="tidb-cluster")
        injector.recover_non_existent_storage()
        print(f"[FAULT RECOVERED] {self.faulty_service} non-existent storage failure\n")
