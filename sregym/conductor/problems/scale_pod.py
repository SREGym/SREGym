"""Scale pod replica to zero problem for the SocialNetwork application."""

import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.scale_pod_zero_mitigation import ScalePodZeroMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ScalePodSocialNet(Problem):
    def __init__(self):
        self.app = SocialNetwork()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        # self.faulty_service = "url-shorten-mongodb"
        self.faulty_service = "user-service"
        # Choose a very front service to test - this will directly cause an exception
        # TODO: We should create more problems with this using different faulty services
        # self.faulty_service = "nginx-thrift"
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "The deployment is scaled to zero replicas, removing all serving pods for this dependency "
                "and producing immediate request failures for traffic that requires this service."
            ),
        )
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = ScalePodZeroMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="scale_pods_to_zero",
            microservices=[self.faulty_service],
        )
        # Terminating the pod may take long time when scaling
        time.sleep(30)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="scale_pods_to_zero",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
