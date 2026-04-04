from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.workload.blueprint_hotel_work import BHotelWrk, BHotelWrkWorkloadManager
from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class LoadSpikeRPCRetryStorm(Problem):
    def __init__(self):
        self.app = BlueprintHotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "rpc"
        self.root_cause = self.build_structured_root_cause(
            component=f"configmap/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "RPC timeout and retry parameters are misconfigured to 50ms and 30 retries, and when workload spikes these "
                "settings trigger retry amplification that overwhelms the service and dramatically increases error rates."
            ),
        )
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.mitigation_oracle = AlertOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
        self.start_workload()

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def create_workload(self, tput: int = None, duration: str = None, multiplier: int = None):
        if tput is None:
            tput = 1000
        if duration is None:
            duration = "600s"
        if multiplier is None:
            multiplier = 6
        self.wrk = BHotelWrkWorkloadManager(
            wrk=BHotelWrk(tput=tput, duration=duration, multiplier=multiplier),
            namespace=self.namespace,
            continuous=True,
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.start()

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()
