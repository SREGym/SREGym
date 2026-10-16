from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.valkey_auth_mitigation import ValkeyAuthMitigation
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ValkeyAuthDisruption(Problem):
    def __init__(self):
        super().__init__(app=AstronomyShop())
        self.faulty_service = "valkey-cart"
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"service/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "Valkey was reconfigured at runtime to require a password (`CONFIG SET requirepass`) that its "
                "clients do not have, so the cart service's unauthenticated connections are rejected with NOAUTH, "
                "the cart service cannot start or serve requests, and cart-related operations fail."
            ),
        )

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ValkeyAuthMitigation(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector._inject(fault_type="valkey_auth_disruption")
        print("[FAULT INJECTED] valkey auth disruption")

    @mark_fault_injected
    def recover_fault(self):
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector._recover(fault_type="valkey_auth_disruption")
        print("[FAULT INJECTED] valkey auth disruption")
