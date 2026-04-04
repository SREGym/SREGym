from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class EnvVariableShadowing(Problem):
    def __init__(self, app_name: str = "astronomy_shop", faulty_service: str = "frontend-proxy"):
        self.faulty_service = faulty_service
        self.app_name = app_name

        if self.app_name == "astronomy_shop":
            self.app = AstronomyShop()
        else:
            raise ValueError(f"Unsupported application: {self.app_name}")
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "Duplicate environment variables shadow expected configuration values (for example FRONTEND_HOST), "
                "so the service resolves dependency endpoints incorrectly and user-facing request flows fail or route "
                "to the wrong backend."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_env_variable_shadowing(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_env_variable_shadowing(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
