"""
This fault specifies an invalid update strategy.
"""

import time
from datetime import datetime, timedelta
from typing import Any

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.operator_misoperation.wrong_update_strategy_mitigation import (
    WrongUpdateStrategyMitigationOracle,
)
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_operator import K8SOperatorFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class K8SOperatorWrongUpdateStrategyFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        app = FleetCast()
        super().__init__(app=app, namespace="tidb-cluster")
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.root_cause = "The TiDBCluster custom resource specifies an invalid update strategy, causing deployment updates to fail or get stuck."
        self.app.create_workload()

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = WrongUpdateStrategyMitigationOracle(problem=self, deployment_name="basic")

    @mark_fault_injected
    def inject_fault(self):
        injector = K8SOperatorFaultInjector(namespace=self.namespace)
        injector.inject_wrong_update_strategy()
        print(f"[FAULT INJECTED] {self.faulty_service} wrong update strategy failure")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = K8SOperatorFaultInjector(namespace=self.namespace)
        injector.recover_wrong_update_strategy()
        print(f"[FAULT RECOVERED] {self.faulty_service} wrong update strategy failure\n")
