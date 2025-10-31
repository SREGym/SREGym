from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.conductor.oracles.detection import DetectionOracle
from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.kubectl import KubeCtl
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.utils.decorators import mark_fault_injected

from sregym.generators.workload.blueprint_hotel_work import BHotelWrk, BHotelWrkWorkloadManager

class GCCapacityDegradation(Problem):
    def __init__(self):
        self.app = BlueprintHotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "garbage collection"

        super().__init__(app=self.app, namespace=self.app.namespace)
        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])


    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_gogc_env_variable_patch(gogc_value="10")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
        self.run_workload()

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_gogc_env_variable_patch()
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def create_workload(
        self, tput: int = None, duration: str = None, multiplier: int = None
    ):
        if tput is None:
            tput = 2000
        if duration is None:
            duration = "500s"
        if multiplier is None:
            multiplier = 6
        self.wrk = BHotelWrkWorkloadManager(
            wrk=BHotelWrk(tput=tput, duration=duration, multiplier=multiplier),
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.start()

    def run_workload(self, namespace='default'):
        self.start_workload()
        job_name = self.wrk.job_name
        self.kubectl.wait_for_job_completion(job_name=job_name, namespace=namespace, timeout=1000)
        workentries = self.wrk.retrievelog()
        workentry = workentries[0] if workentries else None
        print(f"Workload Entry: {workentry}")
        return workentry