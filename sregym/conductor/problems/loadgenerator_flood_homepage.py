"""Otel demo loadgeneratorFloodHomepage feature flag fault."""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_otel import OtelFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class LoadGeneratorFloodHomepage(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.injector = OtelFaultInjector(namespace=self.namespace)
        self.faulty_service = "frontend"  # This fault technically gets injected into the load generator, but the loadgenerator just spams the frontend
        # We can discuss more and see if we think we should change it, but loadgenerator isn't a "real" service.
        self.feature_flag = "loadGeneratorFloodHomepage"
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The `{self.faulty_service}` deployment is experiencing a sustained traffic surge on the "
                "homepage endpoint, saturating frontend capacity. This leads to queueing, high latency, and "
                "timeout spikes during normal user flows. Users observe intermittent homepage errors and degraded "
                "responsiveness across storefront interactions."
            ),
        )
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault(self.feature_flag)
        print(f"Fault: loadgeneratorFloodHomepage | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault(self.feature_flag)
