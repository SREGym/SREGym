"""K8S misconfig fault problem in the SocialNetwork application."""

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.target_port_mitigation import TargetPortMisconfigMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class K8STargetPortMisconfig(Problem):
    def __init__(self, faulty_service="user-service"):
        self.app = SocialNetwork()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"service/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "The Service targetPort is misconfigured from 9090 to 9999, so cluster requests reach no listening "
                "process inside backend pods and callers observe connection failures and upstream timeouts."
            ),
        )

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.resolution_oracle = TargetPortMisconfigMitigationOracle(problem=self)
        self.mitigation_oracle = AlertOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="misconfig_k8s",
            microservices=[self.faulty_service],
        )
        print(f"[FAULT INJECTED] {self.faulty_service} misconfigured")

    @mark_fault_injected
    def recover_fault(self):
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="misconfig_k8s",
            microservices=[self.faulty_service],
        )
        print(f"[FAULT RECOVERED] {self.faulty_service}")
