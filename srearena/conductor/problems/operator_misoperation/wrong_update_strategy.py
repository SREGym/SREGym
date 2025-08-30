"""
This fault specifies an invalid update strategy.
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


class K8SOperatorWrongUpdateStrategyFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        app = FleetCast()
        super().__init__(app = app, namespace='tidb-cluster')
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.app.create_workload()
        
        #Oracles will be attached below
                #self.localization_oracle = MyFutureLocalizationOracle(problem=self, expected=["tidbclusters"])

        # self.mitigation_oracle = MyOracleMitigation(problem=self)


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
