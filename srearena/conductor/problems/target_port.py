"""K8S misconfig fault problem in the SocialNetwork application."""

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


class K8STargetPortMisconfig(Problem):
    def __init__(self, faulty_service="user-service"):
        app = SocialNetwork()
        super().__init__(app=app, namespace=app.namespace)

        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()

        # === Attach evaluation oracles ===
        self.detection_oracle = DetectionOracle(problem=self, expected="Yes")

        self.localization_oracle = LocalizationOracle(problem=self, expected=[faulty_service])

        self.mitigation_oracle = MitigationOracle(problem=self)

        # === Workload setup ===
        self.payload_script = TARGET_MICROSERVICES / "socialNetwork/wrk2/scripts/social-network/compose-post.lua"

    def inject_fault(self):
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="misconfig_k8s",
            microservices=[self.faulty_service],
        )
        print(f"[FAULT INJECTED] {self.faulty_service} misconfigured")

    def recover_fault(self):
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="misconfig_k8s",
            microservices=[self.faulty_service],
        )
        print(f"[FAULT RECOVERED] {self.faulty_service}")

    def start_workload(self):
        print("== Start Workload ==")
        frontend_url = get_frontend_url(self.app)

        wrk = Wrk(rate=10, dist="exp", connections=2, duration=10, threads=2)
        wrk.start_workload(
            payload_script=self.payload_script,
            url=f"{frontend_url}/wrk2-api/post/compose",
        )
