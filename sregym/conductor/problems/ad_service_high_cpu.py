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
        self.feature_flag = "adHighCpu"
        self.cpu_limit = "100m"
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The `{self.faulty_service}` deployment is CPU-throttled due to a tight CPU limit "
                f"(`{self.cpu_limit}`) combined with artificially elevated CPU usage, causing sustained high CPU usage and performance "
                "degradation. Pods for the ad service show increased request latency and intermittent timeouts "
                "under normal storefront traffic. This appears as slower page loads and ad content delays even "
                "when other services remain healthy."
            ),
        )
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault(self.feature_flag)
        # Set a tight CPU limit so the high-CPU flag causes observable throttling.
        patch = (
            '{"spec":{"template":{"spec":{"containers":[{"name":"ad",'
            f'"resources":{{"limits":{{"cpu":"{self.cpu_limit}"}}}}}}]}}}}'
        )
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=strategic -p '{patch}'"
        )
        print(f"Fault: AdServiceHighCpu | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault(self.feature_flag)
        # Remove the CPU limit added during injection.
        patch = '[{"op":"remove","path":"/spec/template/spec/containers/0/resources/limits/cpu"}]'
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=json -p '{patch}'"
        )
