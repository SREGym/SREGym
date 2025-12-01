from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class UpdateIncompatibleCorrelated(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = [
            "mongodb-geo",
            "mongodb-profile",
            "mongodb-rate",
            "mongodb-recommendation",
            "mongodb-reservation",
            "mongodb-user",
        ]
        self.root_cause = "The MongoDB deployments (mongodb-geo, mongodb-profile, mongodb-rate, and mongodb-recommendation) are updated to use an incompatible image version 'mongo:8.0.14-rc0'."
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        super().__init__(app=self.app, namespace=self.namespace)

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        # not really the incorrect image problem, just reuse the incorrect image function
        self.mitigation_oracle = IncorrectImageMitigationOracle(
            problem=self, actual_images={service: "mongo:8.0.14-rc0" for service in self.faulty_service}
        )

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        # not really the incorrect image problem, just reuse the incorrect image function
        for service in self.faulty_service:
            self.injector.inject_incorrect_image(
                deployment_name=service, namespace=self.namespace, bad_image="mongo:8.0.14-rc0"
            )
            print(f"Service: {service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        for service in self.faulty_service:
            self.injector.recover_incorrect_image(
                deployment_name=service,
                namespace=self.namespace,
                correct_image="mongo:4.4.6",
            )
