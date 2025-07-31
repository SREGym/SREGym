"""K8S misconfig fault problem in the SocialNetwork application."""

from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.target_port_mitigation import TargetPortMisconfigMitigationOracle
from srearena.conductor.oracles.workload import WorkloadOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.social_network import SocialNetwork
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class K8STargetPortMisconfig(Problem):
    def __init__(self, faulty_service="user-service"):
        app = SocialNetwork()
        super().__init__(app=app, namespace=app.namespace)

        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()

        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected=[faulty_service])

        self.app.create_workload()
        self.mitigation_oracle = CompoundedOracle(
            self,
            TargetPortMisconfigMitigationOracle(problem=self),
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
        )

    @mark_fault_injected
    def inject_fault(self):
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="misconfig_k8s",
            microservices=[self.faulty_service],
        )
        print(f"[FAULT INJECTED] {self.faulty_service} misconfigured")

    @mark_fault_injected
    def recover_fault(self):
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="misconfig_k8s",
            microservices=[self.faulty_service],
        )
        print(f"[FAULT RECOVERED] {self.faulty_service}")
