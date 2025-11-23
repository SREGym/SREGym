from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.workload import WorkloadOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class RBACMisconfiguration(Problem):
    def __init__(self, app_name: str = "astronomy_shop", faulty_service: str = "frontend"):
        if app_name == "astronomy_shop":
            self.app = AstronomyShop()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service

        super().__init__(app=self.app, namespace=self.app.namespace)
        self.root_cause = f"The deployment `{self.faulty_service}` uses a ServiceAccount with a ClusterRole that lacks ConfigMap permissions, but an init container tries to access a ConfigMap, causing the init container to fail and pods to remain in Init state."

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

        # self.mitigation_oracle = CompoundedOracle(self, WorkloadOracle(problem=self, wrk_manager=self.app.wrk))

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: RBAC Init Container Misconfiguration ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(fault_type="rbac_misconfiguration", microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: RBAC Init Container Misconfiguration ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(fault_type="rbac_misconfiguration", microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
