from kubernetes import client

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.workload.blueprint_hotel_work import BHotelWrk, BHotelWrkWorkloadManager
from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class CapacityDecreaseRPCRetryStorm(Problem):
    def __init__(self):
        self.app = BlueprintHotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "rpc"
        self.root_cause = f"The ConfigMap `{self.faulty_service}` has misconfigured RPC timeout (50ms) and retry settings (30 retries), causing an RPC retry storm that overwhelms the service. It is a metastable failure."
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.mitigation_oracle = AlertOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)

        print("[Step 1] Patching rpc ConfigMap with misconfigured timeout (50ms) and retries (30)...")
        injector.inject_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(
            f"[Step 1] Done — ConfigMap `{self.faulty_service}` patched and pods restarted in namespace `{self.namespace}`\n"
        )

        print("[Step 2] Starting persistent background workload...")
        print("[Step 2] Workload running — waiting 60s before injecting trigger...")
        print(
            "[Step 3] At t+60s: injecting network latency (100ms) + CPU stress to push gRPC calls above 50ms timeout → 31x retry flood"
        )
        print(
            "[Step 3] At t+90s: trigger removed, permanent capacity restraint applied — storm must be self-sustaining before inject_fault returns\n"
        )
        self.start_workload()
        print(
            "[Step 3] Done — metastable state established: retry storm is self-sustaining under permanent capacity restraint\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery (Hard Reboot) ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)

        # 1. Restore rpc ConfigMap (removes misconfigured timeout + retries)
        injector.recover_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(f"[Recovery] rpc ConfigMap restored in namespace {self.namespace}")

        # 2. Remove capacity restraint (ResourceQuota + LimitRange)
        core_v1 = client.CoreV1Api()
        for delete_fn, kind in [
            (core_v1.delete_namespaced_resource_quota, "ResourceQuota"),
            (core_v1.delete_namespaced_limit_range, "LimitRange"),
        ]:
            try:
                delete_fn("capacity-restraint", self.namespace)
                print(f"[Recovery] {kind} 'capacity-restraint' deleted")
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    print(f"[Recovery] Warning deleting {kind}: {e}")

        # 3. Rolling restart all deployments to shed CPU limits and restore full capacity
        apps_v1 = client.AppsV1Api()
        deployments = apps_v1.list_namespaced_deployment(self.namespace)
        restart_ts = __import__("datetime").datetime.now().isoformat()
        for dep in deployments.items:
            patch = {
                "spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restart_ts}}}}
            }
            apps_v1.patch_namespaced_deployment(dep.metadata.name, self.namespace, patch)
        print(f"[Recovery] {len(deployments.items)} deployments restarted — capacity fully restored.")

    def create_workload(self, tput: int = None, duration: str = None, multiplier: int = None):
        if tput is None:
            tput = 3000
        if duration is None:
            duration = "500s"
        if multiplier is None:
            multiplier = 1
        self.wrk = BHotelWrkWorkloadManager(
            wrk=BHotelWrk(tput=tput, duration=duration, multiplier=multiplier),
            namespace=self.namespace,
            CPU_containment=True,
            continuous=True,
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.start()

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()


if __name__ == "__main__":
    problem = CapacityDecreaseRPCRetryStorm()
    # problem.create_workload(tput=3000, duration="500s", multiplier=1)
    # problem.start_workload()
    # problem.inject_fault()
    # The diagnosis and mitigation oracles will be automatically triggered after fault injection.
    # After testing, you can stop the workload and recover the fault:
    # problem.stop_workload()
    problem.recover_fault()
