"""
This misoperation specifies an invalid toleration effect.
"""

import time
from datetime import datetime, timedelta
from typing import Any

from sregym.conductor.oracles.cr_localization_oracle import CustomResourceLocalizationOracle
from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.conductor.oracles.operator_misoperation.invalid_affinity_mitigation import InvalidAffinityMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_operator import K8SOperatorFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class K8SOperatorInvalidAffinityTolerationFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        app = FleetCast()
        print("App's namespace:", app.namespace)
        super().__init__(app=app, namespace="tidb-cluster")
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.app.create_workload()

        self.localization_oracle = CustomResourceLocalizationOracle(
            problem=self, namespace=self.namespace, resource_type="tidbcluster", expected_resource_name="basic"
        )

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
