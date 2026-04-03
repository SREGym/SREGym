"""Otel demo failedReadinessProbe feature flag fault."""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_otel import OtelFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class FailedReadinessProbe(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.injector = OtelFaultInjector(namespace=self.namespace)
        self.faulty_service = "cart"
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "The cart deployment's readiness probe is consistently failing, so Kubernetes marks pods "
                "NotReady and removes them from service endpoints, breaking cart-dependent request paths. "
                "Symptoms include repeated readiness probe failures and the cart endpoint disappearing from service discovery."
            ),
        )
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault("failedReadinessProbe")
        # Add a gRPC readiness probe to the cart deployment so the failed flag
        # causes Kubernetes to mark the pod as not ready.
        patch = (
            '{"spec":{"template":{"spec":{"containers":[{"name":"cart",'
            '"readinessProbe":{"grpc":{"port":8080},"periodSeconds":5,"failureThreshold":2}}]}}}}'
        )
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=strategic -p '{patch}'"
        )
        print(f"Fault: failedReadinessProbe | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault("failedReadinessProbe")
        # Remove the readiness probe added during injection.
        patch = '[{"op":"remove","path":"/spec/template/spec/containers/0/readinessProbe"}]'
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=json -p '{patch}'"
        )
