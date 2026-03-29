from kubernetes import client, config

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.workload.blueprint_hotel_work import BHotelWrk, BHotelWrkWorkloadManager
from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class GCCapacityDegradation(Problem):
    def __init__(self):
        self.app = BlueprintHotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "garbage collection"
        self.root_cause = "All deployments have the GOGC environment variable set to 10 (instead of the default 100), causing aggressive garbage collection that degrades service capacity and performance. This is a metastable failure."
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

    def _apply_memory_limit(self):
        config.load_kube_config()
        core_v1 = client.CoreV1Api()
        limit_range_body = client.V1LimitRange(
            metadata=client.V1ObjectMeta(name="gc-memory-guard"),
            spec=client.V1LimitRangeSpec(
                limits=[
                    client.V1LimitRangeItem(
                        type="Container",
                        default={"memory": "512Mi", "cpu": "500m"},
                        default_request={"memory": "256Mi", "cpu": "100m"},
                        max={"memory": "512Mi", "cpu": "500m"},
                    )
                ]
            ),
        )
        try:
            core_v1.delete_namespaced_limit_range("gc-memory-guard", self.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        core_v1.create_namespaced_limit_range(self.namespace, limit_range_body)
        print(f"[Memory Guard] LimitRange applied: 512Mi memory + 500m CPU max per container in {self.namespace}")

    def _remove_memory_limit(self):
        config.load_kube_config()
        core_v1 = client.CoreV1Api()
        try:
            core_v1.delete_namespaced_limit_range("gc-memory-guard", self.namespace)
            print(f"[Memory Guard] LimitRange removed from {self.namespace}")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._apply_memory_limit()
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        # GOGC patch triggers a rolling restart — pods come up with the memory LimitRange already in effect
        injector.inject_gogc_env_variable_patch(gogc_value="1")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
        self.start_workload()

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        # Remove LimitRange first so the rolling restart triggered by GOGC recovery
        # brings pods up without CPU/memory constraints
        self._remove_memory_limit()
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_gogc_env_variable_patch()
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def create_workload(self, tput: int = None, duration: str = None, multiplier: int = None):
        if tput is None:
            tput = 3000
        if duration is None:
            duration = "500s"
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

    def run_workload(self, namespace="default"):
        self.start_workload()
        job_name = self.wrk.job_name
        self.kubectl.wait_for_job_completion(job_name=job_name, namespace=namespace, timeout=1000)
        workentries = self.wrk.retrievelog()
        workentry = workentries[0] if workentries else None
        print(f"Workload Entry: {workentry}")
        return workentry
