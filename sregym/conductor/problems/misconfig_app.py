"""Roll out a Geo image configured with an incorrect MongoDB port."""

from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.images import HOTEL_GEO_MISCONFIG_IMAGE
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class MisconfigAppHotelRes(Problem):
    def __init__(self):
        super().__init__(app=HotelReservation())
        self.kubectl = KubeCtl()
        self.faulty_service = ["geo"]
        self.root_cause = self.build_structured_root_cause(
            component="deployment/geo",
            namespace=self.namespace,
            description=(
                f"The geo deployment uses {HOTEL_GEO_MISCONFIG_IMAGE}, whose configuration connects to "
                "mongodb-geo:27777 instead of port 27017. Geo panics when the database connection fails, "
                "causing repeated restarts and breaking geo-dependent request paths."
            ),
        )
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = IncorrectImageMitigationOracle(
            problem=self, actual_images={"geo": HOTEL_GEO_MISCONFIG_IMAGE}
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
