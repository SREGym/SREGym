"""Otel demo adServiceHighCpu feature flag fault."""

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_otel import OtelFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class AdServiceHighCpu(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.injector = OtelFaultInjector(namespace=self.namespace)
        self.faulty_service = "ad"
        self.root_cause = f"The `{self.faulty_service}` service has a feature flag enabled that causes high CPU usage, resulting in performance degradation."
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault("adHighCpu")
        # Set a tight CPU limit so the high-CPU flag causes observable throttling.
        patch = '{"spec":{"template":{"spec":{"containers":[{"name":"ad","resources":{"limits":{"cpu":"200m"}}}]}}}}'
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=strategic -p '{patch}'"
        )
        print(f"Fault: AdServiceHighCpu | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault("adHighCpu")
        # Remove the CPU limit added during injection.
        patch = '[{"op":"remove","path":"/spec/template/spec/containers/0/resources/limits/cpu"}]'
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=json -p '{patch}'"
        )
