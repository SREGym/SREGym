"""
This fault specifies an invalid update strategy.
"""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_operator import K8SOperatorFaultInjector
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class K8SOperatorWrongOperatorImage(Problem):
    def __init__(self, faulty_service="tidb-app"):
        self.app = FleetCast()
        super().__init__(app=self.app, namespace="tidb-cluster")
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component="deployment/tidb-operator-controller-manager",
            namespace="tidb-operator",
            description=(
                "The TiDB operator controller is rolled to an incorrect image, breaking reconciliation logic and leaving "
                "managed TiDB resources in unhealthy or stale states."
            ),
        )
        self.app.create_workload()

        # ============ Attach Evaluation Oracles ============
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        injector = K8SOperatorFaultInjector(namespace=self.namespace)
        injector.inject_wrong_operator_image()
        print(f"[FAULT INJECTED] {self.faulty_service} wrong operator image")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = K8SOperatorFaultInjector(namespace=self.namespace)
        injector.recover_wrong_operator_image()
        print(f"[FAULT RECOVERED] {self.faulty_service} wrong operator image\n")
