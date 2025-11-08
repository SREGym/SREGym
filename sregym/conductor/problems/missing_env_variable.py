from sregym.conductor.oracles.deployment_itself_localization_oracle import DeploymentItselfLocalizationOracle
from sregym.conductor.oracles.localization import LocalizationOracle
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

        if self.app_name == "astronomy_shop":
            self.app = AstronomyShop()
            self.env_var = "CART_ADDR"
            self.env_var_value = "cart:8080"
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.localization_oracle = DeploymentItselfLocalizationOracle(
            problem=self, namespace=self.namespace, expected_deployment_names=[self.faulty_service]
        )

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
