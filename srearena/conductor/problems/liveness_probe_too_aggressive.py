from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.sustained_readiness import SustainedReadinessOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected
from srearena.utils.randomizer import Randomizer

class LivenessProbeTooAggressive(Problem):
    def __init__(self):
        self.kubectl = KubeCtl()
        self.randomizer = Randomizer(self.kubectl)
        app = self.randomizer.select_app()
        super().__init__(app=app, namespace=app.namespace)
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        if self.namespace == "astronomy-shop":
            self.app.create_workload()
        else:
            self.app.create_workload(duration=30)

    def decide_targeted_service(self):
        self.faulty_service = "aux-service"

        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])
        self.mitigation_oracle = CompoundedOracle(
            self,
            SustainedReadinessOracle(self, sustained_period=30)
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_liveness_probe_too_aggressive([self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.app.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_liveness_probe_too_aggressive([self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.app.namespace}\n")
