"""Scale pod replica to zero problem for the SocialNetwork application."""

import time

from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.scale_pod_zero_mitigation import ScalePodZeroMitigationOracle
from srearena.conductor.oracles.workload import WorkloadOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.socialnet import SocialNetwork
from srearena.service.kubectl import KubeCtl


class ScalePodSocialNet(Problem):
    def __init__(self):
        self.app = SocialNetwork()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        # self.faulty_service = "url-shorten-mongodb"
        self.faulty_service = "user-service"
        # Choose a very front service to test - this will directly cause an exception
        # TODO: We should create more problems with this using different faulty services
        # self.faulty_service = "nginx-thrift"
        # === Attach evaluation oracles ===
        self.detection_oracle = DetectionOracle(problem=self, expected="Yes")

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

        self.app.create_workload()
        self.mitigation_oracle = CompoundedOracle(
            self,
            ScalePodZeroMitigationOracle(problem=self),
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
        )

    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="scale_pods_to_zero",
            microservices=[self.faulty_service],
        )
        # Terminating the pod may take long time when scaling
        time.sleep(30)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="scale_pods_to_zero",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
