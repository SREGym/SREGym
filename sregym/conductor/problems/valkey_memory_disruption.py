from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ValkeyMemoryDisruption(Problem):
    def __init__(self):
        app = AstronomyShop()
        super().__init__(app=app, namespace=app.namespace)

        self.faulty_service = "valkey-cart"
        self.kubectl = KubeCtl()
        self.root_cause = "A job is flooding the valkey-cart store with large payloads (10MB each), causing it to enter an out-of-memory (OOM) state."

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

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
