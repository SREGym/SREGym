from srearena.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.rpc_retry_storm_mitigation import RPCRetryStormMitigationOracle
from srearena.conductor.problems.base import Problem
from srearena.service.kubectl import KubeCtl
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.utils.decorators import mark_fault_injected

from srearena.generators.workload.blueprint_hotel_work import BHotelWrk, BHotelWrkWorkloadManager

class CapacityDecreaseRPCRetryStorm(Problem):
    def __init__(self):
        self.app = BlueprintHotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "rpc"

        super().__init__(app=self.app, namespace=self.app.namespace)
        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

        self.mitigation_oracle = RPCRetryStormMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
        self.mitigation_oracle.run_workload(problem=self, kubectl=self.kubectl)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def create_workload(
        self, tput: int = None, duration: str = None, multiplier: int = None
    ):
        if tput is None:
            tput = 3000
        if duration is None:
            duration = "500s"
        if multiplier is None:
            multiplier = 1
        self.wrk = BHotelWrkWorkloadManager(
            wrk=BHotelWrk(tput=tput, duration=duration, multiplier=multiplier),
            CPU_containment=True,
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.start()