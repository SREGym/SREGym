from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class IncorrectImage(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = ["product-catalog"]
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        self.root_cause = (
            "The 'product-catalog' deployment is mis-configured to pull the non-existent image 'app-image:latest'."
        )
        super().__init__(app=self.app, namespace=self.namespace)

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = IncorrectImageMitigationOracle(
            problem=self, actual_images={"product-catalog": "app-image:latest"}
        )

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        for service in self.faulty_service:
            self.injector.inject_incorrect_image(
                deployment_name=service, namespace=self.namespace, bad_image="app-image:latest"
            )
            print(f"Service: {service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        for service in self.faulty_service:
            self.injector.recover_incorrect_image(
                deployment_name=service,
                namespace=self.namespace,
                correct_image="ghcr.io/open-telemetry/demo:2.0.2-productcatalogservice",
            )
