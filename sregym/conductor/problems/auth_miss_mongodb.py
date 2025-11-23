"""MongoDB authentication missing problem in the SocialNetwork application."""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class MongoDBAuthMissing(Problem):
    def __init__(self):
        self.app = SocialNetwork()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "url-shorten-mongodb"
        self.root_cause = f"The MongoDB service `{self.faulty_service}` is configured to require TLS authentication, but the certificates are not properly configured, causing connection failures."
        super().__init__(app=self.app, namespace=self.app.namespace)
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="auth_miss_mongodb",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="auth_miss_mongodb",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
