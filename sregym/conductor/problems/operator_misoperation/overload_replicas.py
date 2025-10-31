# Ramifications: The TiDB cluster can become unhealthy:
# $ kubectl get events -n tidb-cluster
# 10m         Warning   Unhealthy              pod/basic-tidb-0                                                   Readiness probe failed: dial tcp 10.244.0.27:4000: connect: connection refused

# Only a few pods (e.g., 4 out of 100,000 replicas requested) are created successfully.


import time
from datetime import datetime, timedelta
from typing import Any

from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_operator import K8SOperatorFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected
from sregym.conductor.oracles.operator_misoperation.overload_replicas_mitigation import OverloadReplicasMitigationOracle
from sregym.conductor.oracles.localization import LocalizationOracle


class K8SOperatorOverloadReplicasFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        app = FleetCast()
        super().__init__(app= app, namespace='tidb-cluster')
        self.faulty_service= faulty_service
        self.kubectl = KubeCtl()
        self.app.create_workload()
        self.localization_oracle = LocalizationOracle(problem=self, expected=["tidb-cluster"])
        self.mitigation_oracle = OverloadReplicasMitigationOracle(problem=self, deployment_name="basic")
       

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = K8SOperatorFaultInjector(namespace='tidb-cluster')
        injector.inject_overload_replicas()
        print(f"[FAULT INJECTED] {self.faulty_service} overload replica failure\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = K8SOperatorFaultInjector(namespace='tidb-cluster')
        injector.recover_overload_replicas()
        print(f"[FAULT RECOVERED] {self.faulty_service} overload replica failure\n")
