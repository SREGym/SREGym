"""Wrong binary usage problem in the HotelReservation application."""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.wrong_bin_mitigation import WrongBinMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class WrongBinUsage(Problem):
    def __init__(self, faulty_service: str = "profile"):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self.root_cause = f"The deployment `{self.faulty_service}` is configured to use the wrong binary (geo instead of profile), causing the service to malfunction."

        self.app.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )
        super().__init__(app=self.app, namespace=self.app.namespace)
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = WrongBinMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="wrong_bin_usage",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="wrong_bin_usage",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
