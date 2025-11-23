"""Otel demo paymentServiceFailure feature flag fault."""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_otel import OtelFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class PaymentServiceFailure(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.injector = OtelFaultInjector(namespace=self.namespace)
        self.faulty_service = "payment"
        self.root_cause = f"The `{self.faulty_service}` service has a feature flag enabled that causes it to fail, resulting in service unavailability."
        super().__init__(app=self.app, namespace=self.app.namespace)
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault("paymentFailure")
        print(f"Fault: paymentServiceFailure | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault("paymentFailure")
