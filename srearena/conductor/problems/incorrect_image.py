from srearena.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from srearena.conductor.oracles.localization_oracle import LocalizationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_app import ApplicationFaultInjector
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class IncorrectImage(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = ["product-catalog"]
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        super().__init__(app=self.app, namespace=self.namespace)

        self.localization_oracle = LocalizationOracle(problem=self, expected=self.faulty_service)
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
