# Ramifications: The TiDB cluster can become unhealthy:
# $ kubectl get events -n tidb-cluster
# 10m         Warning   Unhealthy              pod/basic-tidb-0                                                   Readiness probe failed: dial tcp 10.244.0.27:4000: connect: connection refused

# Only a few pods (e.g., 4 out of 100,000 replicas requested) are created successfully.


import time
from datetime import datetime, timedelta
from typing import Any

from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_operator import K8SOperatorFaultInjector
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.fleet_cast import FleetCast
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected

class K8SOperatorOverloadReplicasFault(Problem):
    def __init__(self, faulty_service="tidb-app"):
        app = FleetCast()
        super().__init__(app= app, namespace='tidb-cluster')
        self.faulty_service= faulty_service
        self.kubectl = KubeCtl()
        self.app.create_workload()
            #Oracles will be attached below
                #self.localization_oracle = MyFutureLocalizationOracle(problem=self, expected=["tidbclusters"])

        # self.mitigation_oracle = MyOracleMitigation(problem=self)
       

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = K8SOperatorFaultInjector(namespace=self.namespace)
        injector.inject_overload_replicas
        print(f"[FAULT INJECTED] {self.faulty_service} overload replica failure\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = K8SOperatorFaultInjector(namespace=self.namespace)
        injector.recover_overload_replicas()
        print(f"[FAULT RECOVERED] {self.faulty_service} overload replica failure\n")



#USE BELOW FOR ORACLE REFERENCE

# ################## Detection Problem ##################
# class K8SOperatorOverloadReplicasDetection(K8SOperatorOverloadReplicasBaseTask, DetectionTask):
#     def __init__(self):
#         K8SOperatorOverloadReplicasBaseTask.__init__(self)
#         DetectionTask.__init__(self, self.app)

#     def eval(self, soln: Any, trace: list[SessionItem], duration: float):
#         print("== Evaluation ==")
#         expected_solution = "Yes"

#         if isinstance(soln, str):
#             if soln.strip().lower() == expected_solution.lower():
#                 print(f"Correct detection: {soln}")
#                 self.add_result("Detection Accuracy", "Correct")
#             else:
#                 print(f"Incorrect detection: {soln}")
#                 self.add_result("Detection Accuracy", "Incorrect")
#         else:
#             print("Invalid solution format")
#             self.add_result("Detection Accuracy", "Invalid Format")

#         return super().eval(soln, trace, duration)


# ################## Localization Problem ##################
# class K8SOperatorOverloadReplicasLocalization(K8SOperatorOverloadReplicasBaseTask, LocalizationTask):
#     def __init__(self):
#         K8SOperatorOverloadReplicasBaseTask.__init__(self)
#         LocalizationTask.__init__(self, self.app)
#         self.task_desc = """\
#             You are an expert DevOps engineer who has been tasked with localizing faults in a deployed service.

#             The service you are working with today is described below:
#             {app_summary}

#             You will begin by analyzing the service's state and telemetry, and then submit one of two possible solutions:
#             1. list[str]: list of faulty components or custom resources (e.g., service names, CRs)
#             2. str: `None` if no faults were detected
#             """

#     def eval(self, soln: Any, trace: list[SessionItem], duration: float):
#         print("== Evaluation ==")

#         if soln is None:
#             print("Solution is None")
#             self.add_result("Localization Accuracy", 0.0)
#             self.results["success"] = False
#             self.results["is_subset"] = False
#             super().eval(soln, trace, duration)
#             return self.results

#         # Calculate exact match and subset
#         is_exact = is_exact_match(soln, self.faulty_cr)
#         is_sub = is_subset([self.faulty_cr], soln)

#         # Determine accuracy
#         if is_exact:
#             accuracy = 100.0
#             print(f"Exact match: {soln} | Accuracy: {accuracy}%")
#         elif is_sub:
#             accuracy = (len([self.faulty_cr]) / len(soln)) * 100.0
#             print(f"Subset match: {soln} | Accuracy: {accuracy:.2f}%")
#         else:
#             accuracy = 0.0
#             print(f"No match: {soln} | Accuracy: {accuracy}%")

#         self.add_result("Localization Accuracy", accuracy)
#         super().eval(soln, trace, duration)

#         self.results["success"] = is_exact or (is_sub and len(soln) == 1)
#         self.results["is_subset"] = is_sub

#         return self.results
