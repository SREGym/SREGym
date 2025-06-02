"""Scale pod replica to zero problem for the SocialNetwork application."""

import time

from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.generators.workload.wrk import Wrk
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.socialnet import SocialNetwork
from srearena.service.kubectl import KubeCtl

from .helpers import get_frontend_url


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
        self.payload_script = TARGET_MICROSERVICES / "socialNetwork/wrk2/scripts/social-network/compose-post.lua"
        # === Attach evaluation oracles ===
        self.detection_oracle = DetectionOracle(problem=self, expected="Yes")

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

        self.mitigation_oracle = MitigationOracle(problem=self)

        # === Workload setup ===
        self.payload_script = TARGET_MICROSERVICES / "socialNetwork/wrk2/scripts/social-network/compose-post.lua"

    def start_workload(self):
        print("== Start Workload ==")
        frontend_url = get_frontend_url(self.app)

        wrk = Wrk(rate=10, dist="exp", connections=2, duration=10, threads=2)
        wrk.start_workload(
            payload_script=self.payload_script,
            url=f"{frontend_url}/wrk2-api/post/compose",
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
