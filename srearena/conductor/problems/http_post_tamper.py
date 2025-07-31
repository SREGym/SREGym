from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_symp import SymptomFaultInjector
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class ChaosMeshHttpPostTamper(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "email"
        self.injector = SymptomFaultInjector(namespace=self.namespace)
        super().__init__(app=self.app, namespace=self.namespace)
        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_http_post_tamper(component_label="email", port=8080)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_http_post_tamper(component_label="email")
