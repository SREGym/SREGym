"""Otel demo emailMemoryLeak feature flag fault."""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_otel import OtelFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class EmailMemoryLeak(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.injector = OtelFaultInjector(namespace=self.namespace)
        self.faulty_service = "email"
        self.root_cause = "The `email` service has the `emailMemoryLeak` feature flag enabled, which simulates a memory leak in the email service."
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault("emailMemoryLeak")
        print(f"Fault: emailMemoryLeak | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault("emailMemoryLeak")
