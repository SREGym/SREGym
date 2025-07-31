from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_app import ApplicationFaultInjector
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class ValkeyMemoryDisruption(Problem):
    def __init__(self):
        app = AstronomyShop()
        super().__init__(app=app, namespace=app.namespace)

        self.faulty_service = "valkey-cart"
        self.kubectl = KubeCtl()

        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected="valkey")

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector._inject(fault_type="valkey_memory_disruption")
        print(f"[FAULT INJECTED] valkey auth disruption")

    @mark_fault_injected
    def recover_fault(self):
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector._recover(fault_type="valkey_memory_disruption")
        print(f"[FAULT INJECTED] valkey memory disruption")
