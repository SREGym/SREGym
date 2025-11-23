from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
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
        self.root_cause = f"The valkey-cart service has an invalid password configured, causing authentication failures for dependent services."

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
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
