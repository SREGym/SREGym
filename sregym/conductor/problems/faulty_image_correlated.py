from sregym.conductor.oracles.deployment_itself_localization_oracle import DeploymentItselfLocalizationOracle
from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class FaultyImageCorrelated(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = ["frontend", "geo", "profile", "rate", "recommendation", "reservation", "user", "search"]
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        super().__init__(app=self.app, namespace=self.namespace)

        self.localization_oracle = DeploymentItselfLocalizationOracle(
            problem=self, namespace=self.namespace, expected_deployment_names=self.faulty_service
        )
        # not really the incorrect image problem, just reuse the incorrect image function
        self.mitigation_oracle = IncorrectImageMitigationOracle(
            problem=self,
            actual_images={service: "jackcuii/hotel-reservation:latest" for service in self.faulty_service},
        )

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        # not really the incorrect image problem, just reuse the incorrect image function
        for service in self.faulty_service:
            self.injector.inject_incorrect_image(
                deployment_name=service, namespace=self.namespace, bad_image="jackcuii/hotel-reservation:latest"
            )
            print(f"Service: {service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        for service in self.faulty_service:
            self.injector.recover_incorrect_image(
                deployment_name=service,
                namespace=self.namespace,
                correct_image="yinfangchen/hotel-reservation:latest",
            )
