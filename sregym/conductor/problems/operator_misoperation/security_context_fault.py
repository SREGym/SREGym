"""
The fault sets an invalid runAsUser value.
"""

import time
from datetime import datetime, timedelta
from typing import Any

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.operator_misoperation.security_context_mitigation import SecurityContextMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_operator import K8SOperatorFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class K8SOperatorSecurityContextFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        app = FleetCast()
        super().__init__(app=app, namespace="tidb-cluster")
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.root_cause = "The TiDBCluster custom resource specifies an invalid runAsUser value in the security context, causing pods to fail to start or be rejected by the security policy."
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
