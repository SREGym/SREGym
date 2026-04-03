from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.missing_env_variable_mitigation import MissingEnvVariableMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class MissingEnvVariable(Problem):
    def __init__(self, app_name: str = "astronomy_shop", faulty_service: str = "frontend"):
        self.faulty_service = faulty_service
        self.app_name = app_name

        if self.app_name != "astronomy_shop":
            raise ValueError

        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.env_var = "CART_ADDR"
        self.env_var_value = "cart:8080"
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The required environment variable {self.env_var} is removed from the deployment, so the application "
                "cannot resolve the cart dependency endpoint and frontend flows fail. "
                "Symptoms include runtime configuration errors and partial frontend outages for cart-related actions."
            ),
        )

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MissingEnvVariableMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.inject_missing_env_variable(
            deployment_name=self.faulty_service,
            env_var=self.env_var,
        )

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.recover_missing_env_variable(
            deployment_name=self.faulty_service,
            env_var=self.env_var,
            env_value=self.env_var_value,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
