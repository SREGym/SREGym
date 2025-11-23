"""Otel demo loadgeneratorFloodHomepage feature flag fault."""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_otel import OtelFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class LoadGeneratorFloodHomepage(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.injector = OtelFaultInjector(namespace=self.namespace)
        self.faulty_service = "frontend"  # This fault technically gets injected into the load generator, but the loadgenerator just spams the frontend
        # We can discuss more and see if we think we should change it, but loadgenerator isn't a "real" service.
        self.root_cause = "The load generator has a feature flag enabled that causes it to flood the homepage with excessive requests, overwhelming the frontend service."
        super().__init__(app=self.app, namespace=self.app.namespace)
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault("loadGeneratorFloodHomepage")
        print(f"Fault: loadgeneratorFloodHomepage | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault("loadGeneratorFloodHomepage")
