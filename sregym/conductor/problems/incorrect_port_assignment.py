from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class IncorrectPortAssignment(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "checkout"
        self.env_var = "PRODUCT_CATALOG_ADDR"
        self.incorrect_port = "8082"
        self.correct_port = "8080"
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        self.root_cause = f"The deployment `{self.faulty_service}` has the environment variable `{self.env_var}` configured with an incorrect port `{self.incorrect_port}` instead of `{self.correct_port}`."
        super().__init__(app=self.app, namespace=self.namespace)
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = IncorrectPortAssignmentMitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_incorrect_port_assignment(
            deployment_name=self.faulty_service,
            component_label=self.faulty_service,
            env_var=self.env_var,
            incorrect_port=self.incorrect_port,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_incorrect_port_assignment(
            deployment_name="checkout", env_var=self.env_var, correct_port="8080"
        )
