"""
This fault specifies a non-existent storage class.
"""

import time
from datetime import datetime, timedelta
from typing import Any

from sregym.conductor.oracles.cr_localization_oracle import CustomResourceLocalizationOracle
from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.conductor.oracles.operator_misoperation.non_existent_storage_mitigation import (
    NonExistentStorageClassMitigationOracle,
)
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_operator import K8SOperatorFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class K8SOperatorNonExistentStorageFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        app = FleetCast()
        super().__init__(app=app, namespace="tidb-cluster")
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.problem_id = "operator_non_existent_storage"
        self.localization_oracle = CustomResourceLocalizationOracle(
            problem=self, namespace=self.namespace, resource_type="tidbcluster", expected_resource_name="basic"
        )
        self.mitigation_oracle = NonExistentStorageClassMitigationOracle(problem=self, deployment_name="basic")

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
