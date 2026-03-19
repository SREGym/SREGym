"""MongoDB storage user unregistered problem in the HotelReservation application."""

from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class MisconfigAppHotelRes(Problem):
    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = ["geo"]
        self.root_cause = "The 'geo' deployment is configured to use a buggy container image 'yinfangchen/geo:app3', this causes the pod keep restarting and entering the 'Error' state."
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = IncorrectImageMitigationOracle(
            problem=self, actual_images={"geo": "yinfangchen/geo:app3"}
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="misconfig_app",
            microservices=self.faulty_service,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="misconfig_app",
            microservices=self.faulty_service,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
