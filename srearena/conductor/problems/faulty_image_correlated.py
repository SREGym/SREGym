from srearena.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from srearena.conductor.oracles.localization_oracle import LocalizationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_app import ApplicationFaultInjector
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class FaultyImageCorrelated(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = ["frontend", "geo", "profile", "rate", "recommendation", "reservation", "user", "search"]
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        super().__init__(app=self.app, namespace=self.namespace)

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])
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
