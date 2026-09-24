import json
import subprocess
import time

from kubernetes import client, config

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.workload.blueprint_hotel_work import BHotelWrk, BHotelWrkWorkloadManager
from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

MAX_WORKLOAD_REPLICAS = 8
CALIBRATION_SETTLE_SECONDS = 150
CALIBRATION_PROBE_COUNT = 3
CALIBRATION_PROBE_INTERVAL = 15
_PROMETHEUS_URL = "http://localhost:9090"
# After the 60s warm-up + 30s spike, the wlgen reverts to base traffic
# for the remainder of DURATION (3600s total → 3510s of base traffic).
_REVERT_SECONDS = 3510


class LoadSpikeRPCRetryStorm(Problem):
    def __init__(self):
        super().__init__(app=BlueprintHotelReservation())
        self.kubectl = KubeCtl()
        self.faulty_service = "rpc"
        self.root_cause = self.build_structured_root_cause(
            component=f"configmap/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "The RPC configuration has an unrealistically low timeout (50ms) with very high retries (30). "
                "Under heavy load (high multiplier), the combination triggers massive retry amplification that "
                "pushes the system into a self-sustaining metastable retry storm even after the transient trigger is removed."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self, exclude_alerts=["HighRequestRate"])

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)

        print("[Step 1] Patching rpc ConfigMap with misconfigured timeout (50ms) and retries (30)...")
        injector.inject_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(
            f"[Step 1] Done — ConfigMap `{self.faulty_service}` patched and pods restarted in namespace `{self.namespace}`\n"
        )

        print("[Step 2] Starting workload with one-shot spike trigger...")
        print("[Step 2] 60s warm-up → 30s spike (tput×multiplier) → base traffic only")
        print("[Step 2] The spike pushes gRPC calls above 50ms timeout → 31x retry flood")
        print("[Step 2] After spike: retry storm self-sustains at base traffic alone\n")
        self.start_workload()
        print(
            "[Step 2] Done — metastable state established: retry storm is self-sustaining at base traffic without spike\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(f"[Recovery] rpc ConfigMap restored in namespace {self.namespace}")

    def create_workload(self, tput: int = None, duration: str = None, multiplier: int = None):
        if tput is None:
            tput = 3000
        if duration is None:
            duration = "3600s"
        if multiplier is None:
            multiplier = 6
        self.wrk = BHotelWrkWorkloadManager(
            wrk=BHotelWrk(tput=tput, duration=duration, multiplier=multiplier),
            namespace=self.namespace,
            CPU_containment=True,
            continuous=True,
            apply_capacity_restraint=False,
        )

    def _configure_single_spike(self):
        """Patch wlgen ConfigMap so the spike is a one-shot trigger followed by long base traffic."""
        config.load_kube_config()
        client.CoreV1Api().patch_namespaced_config_map(
            name="bhotelwrk-wlgen-env",
            namespace=self.namespace,
            body={"data": {"REVERTTIME": str(_REVERT_SECONDS)}},
        )
        subprocess.run(
            ["kubectl", "rollout", "restart", "deployment", "bhotelwrk-wlgen", "-n", self.namespace],
            check=True,
        )
        subprocess.run(
            ["kubectl", "rollout", "status", "deployment", "bhotelwrk-wlgen", "-n", self.namespace, "--timeout=120s"],
            check=True,
        )
        print(f"[Config] Wlgen set for single spike: 60s warm-up → 30s spike → {_REVERT_SECONDS}s base traffic")

    def _scale_workload_deployment(self, replicas: int):
        config.load_kube_config()
        apps_v1 = client.AppsV1Api()
        apps_v1.patch_namespaced_deployment(
            name="bhotelwrk-wlgen",
            namespace=self.namespace,
            body={"spec": {"replicas": replicas}},
        )
        print(f"[Load Spike] Scaled workload deployment to {replicas} replicas")

    def _query_high_request_latency_alert(self) -> bool:
        """Return True if HighRequestLatency is firing for this namespace."""
        cmd = [
            "kubectl",
            "exec",
            "-n",
            "observe",
            "deploy/prometheus-server",
            "-c",
            "prometheus-server",
            "--",
            "wget",
            "-qO-",
            f"{_PROMETHEUS_URL}/api/v1/alerts",
        ]
        try:
            raw = subprocess.check_output(cmd, text=True, timeout=15)
            payload = json.loads(raw)
        except Exception:
            return False

        for alert in payload.get("data", {}).get("alerts", []):
            if alert.get("state") != "firing":
                continue
            labels = alert.get("labels", {})
            if labels.get("namespace") == self.namespace and labels.get("alertname") == "HighRequestLatency":
                return True
        return False

    def _calibrate_workload_replicas(self):
        """Scale workload replicas up until HighRequestLatency fires and sustains.

        Starts from the current replica count (1) and increments until the
        Prometheus HighRequestLatency alert is observed firing in at least
        2 out of 3 consecutive probes.  Because the calibration runs during
        the REVERTTIME phase (base traffic only, spike already ended), a
        passing result proves the retry storm is self-sustaining without
        the spike trigger.
        """
        current_replicas = 1

        for _ in range(MAX_WORKLOAD_REPLICAS):
            print(
                f"[Calibration] Waiting {CALIBRATION_SETTLE_SECONDS}s for metrics to stabilize "
                f"({current_replicas} replica(s))..."
            )
            time.sleep(CALIBRATION_SETTLE_SECONDS)

            firing_count = 0
            for i in range(CALIBRATION_PROBE_COUNT):
                if self._query_high_request_latency_alert():
                    firing_count += 1
                if i < CALIBRATION_PROBE_COUNT - 1:
                    time.sleep(CALIBRATION_PROBE_INTERVAL)

            print(
                f"[Calibration] HighRequestLatency fired in {firing_count}/{CALIBRATION_PROBE_COUNT} probes "
                f"at {current_replicas} replica(s)"
            )

            if firing_count >= 2:
                print(f"[Calibration] Metastable state confirmed at {current_replicas} replica(s)")
                return

            current_replicas += 1
            if current_replicas > MAX_WORKLOAD_REPLICAS:
                break
            print(f"[Calibration] Not sustained — scaling to {current_replicas} replica(s)...")
            self._scale_workload_deployment(current_replicas)

        print(f"[Calibration] WARNING: HighRequestLatency not sustained even at {MAX_WORKLOAD_REPLICAS} replicas")

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.create_task()
        self._configure_single_spike()
        self._scale_workload_deployment(1)
        self.wrk._run_cpu_containment_sequence()
        self._calibrate_workload_replicas()

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()


if __name__ == "__main__":
    problem = LoadSpikeRPCRetryStorm()
    problem.recover_fault()
