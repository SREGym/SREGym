"""
This misoperation specifies an invalid toleration effect.
"""

import time
from datetime import datetime, timedelta
from typing import Any

from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_operator import K8SOperatorFaultInjector
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.fleet_cast import FleetCast
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.operator_misoperation.invalid_affinity_mitigation import InvalidAffinityMitigationOracle  

class K8SOperatorInvalidAffinityTolerationFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        app = FleetCast()
        print("App's namespace:", app.namespace)
        super().__init__(app=app, namespace='tidb-cluster')
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.app.create_workload()
               
        self.localization_oracle = LocalizationOracle(problem=self, expected=["tidb-cluster"])

        self.mitigation_oracle = InvalidAffinityMitigationOracle(problem=self, deployment_name="basic")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = K8SOperatorFaultInjector(namespace='tidb-cluster')
        injector.inject_invalid_affinity_toleration()
        print(f"[FAULT INJECTED] {self.faulty_service} invalid affinity toleration failure\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        
        injector = K8SOperatorFaultInjector(namespace='tidb-cluster')
        injector.recover_invalid_affinity_toleration()
        print(f"[FAULT INJECTED] {self.faulty_service} invalid affinity toleration failure\n")
