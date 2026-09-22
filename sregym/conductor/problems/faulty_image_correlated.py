from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.images import HOTEL_CORRELATED_FAULT_IMAGE
from sregym.service.apps.hotel_reservation import HOTEL_RESERVATION_APPLICATION_IMAGE, HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class FaultyImageCorrelated(Problem):
    def __init__(self):
        super().__init__(app=HotelReservation())
        self.kubectl = KubeCtl()
        self.faulty_service = ["frontend", "geo", "profile", "rate", "recommendation", "reservation", "user", "search"]
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        self.root_cause = self.build_structured_root_cause(
            component=f"deployments/{', '.join(self.faulty_service)}",
            namespace=self.namespace,
            description=(
                f"A correlated bad rollout pins multiple core services to {HOTEL_CORRELATED_FAULT_IMAGE}. "
                "This image contains Social Network rather than Hotel Reservation, so the Hotel startup "
                "commands are missing. The affected containers cannot start, breaking the application path."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = IncorrectImageMitigationOracle(
            problem=self,
            actual_images={service: HOTEL_CORRELATED_FAULT_IMAGE for service in self.faulty_service},
        )

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        for service in self.faulty_service:
            self.injector.inject_incorrect_image(
                deployment_name=service, namespace=self.namespace, bad_image=HOTEL_CORRELATED_FAULT_IMAGE
            )
            print(f"Service: {service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        for service in self.faulty_service:
            self.injector.recover_incorrect_image(
                deployment_name=service,
                namespace=self.namespace,
                correct_image=HOTEL_RESERVATION_APPLICATION_IMAGE,
            )
