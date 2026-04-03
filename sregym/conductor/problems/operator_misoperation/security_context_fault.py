"""
The fault sets an invalid runAsUser value.
"""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.operator_misoperation.security_context_mitigation import SecurityContextMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_operator import K8SOperatorFaultInjector
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class K8SOperatorSecurityContextFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        self.app = FleetCast()
        super().__init__(app=self.app, namespace="tidb-cluster")
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component="customresource/tidbcluster/basic",
            namespace="tidb-cluster",
            description=(
                "The TiDBCluster pod security context sets an invalid runAsUser value, so workload pods are rejected by policy "
                "or fail during startup due to security context violations."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = SecurityContextMitigationOracle(problem=self, deployment_name="basic")
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        injector = K8SOperatorFaultInjector(namespace=self.namespace)
        injector.inject_security_context_fault()
        print(f"[FAULT INJECTED] {self.faulty_service} security context misconfigured")

    @mark_fault_injected
    def recover_fault(self):
        injector = K8SOperatorFaultInjector(namespace=self.namespace)
        injector.recover_security_context_fault()
        print(f"[FAULT RECOVERED] {self.faulty_service}")
