from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.conductor.oracles.pod_of_deployment_oracle import PodOfDeploymentOracle
from sregym.conductor.oracles.valkey_auth_mitigation import ValkeyAuthMitigation
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ValkeyAuthDisruption(Problem):
    def __init__(self):
        app = AstronomyShop()
        super().__init__(app=app, namespace=app.namespace)

        self.faulty_service = "valkey-cart"
        self.kubectl = KubeCtl()

        # === Attach evaluation oracles ===
        self.localization_oracle = PodOfDeploymentOracle(
            problem=self, namespace=self.namespace, expected_deployment_name=self.faulty_service
        )
        self.mitigation_oracle = ValkeyAuthMitigation(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector._inject(fault_type="valkey_auth_disruption")
        print(f"[FAULT INJECTED] valkey auth disruption")

    @mark_fault_injected
    def recover_fault(self):
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector._recover(fault_type="valkey_auth_disruption")
        print(f"[FAULT INJECTED] valkey auth disruption")
