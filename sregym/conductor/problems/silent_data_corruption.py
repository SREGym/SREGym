from enum import StrEnum
import json
import time
from typing import Optional

from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_kernel import KernelInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected
from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.service.dm_flakey_manager import DM_FLAKEY_DEVICE_NAME, DmFlakeyManager
from sregym.conductor.oracles.workload import WorkloadOracle


class SilentDataCorruptionStrategy(StrEnum):
    READ_CORRUPT = "read_corrupt"
    WRITE_CORRUPT = "write_corrupt"
    BOTH_CORRUPT = "both_corrupt"


class SilentDataCorruption(Problem):

    def __init__(
        self,
        target_deploy: str = "mongodb-geo",
        namespace: str = "hotel-reservation",
        strategy: SilentDataCorruptionStrategy = SilentDataCorruptionStrategy.BOTH_CORRUPT,
        probability: int = 100, # (0-100)% probability 
        up_interval: int = 0,  # Seconds device is healthy
        down_interval: int = 1,  # Seconds device corrupts data
    ):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = namespace
        self.deploy = target_deploy
        self.injector = KernelInjector(self.kubectl)
        self.dm_flakey_manager = DmFlakeyManager(self.kubectl)
        self.target_node: Optional[str] = None
        self.strategy = strategy
        self.probability = probability
        self.up_interval = up_interval
        self.down_interval = down_interval
        self.probability = self.probability * 10000000 # (0-1000000000 scale) for (0-100% probability)

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.root_cause = "There's a silent data corruption on the hard drive that the mongodb-geo service's data is on."

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

    def requires_khaos(self) -> bool:
        """This problem requires Khaos for dm-flakey infrastructure setup."""
        return True

    def _discover_node_for_deploy(self) -> Optional[str]:
        """Return the node where the target deployment is running."""
        # First try with a label selector (common OpenEBS hotel-reservation pattern)
        svc = self.deploy.split("-", 1)[-1]  # e.g. "geo"
        cmd = f"kubectl -n {self.namespace} get pods -l app=mongodb,component={svc} -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        data = json.loads(out or "{}")
        for item in data.get("items", []):
            if item.get("status", {}).get("phase") == "Running":
                return item["spec"]["nodeName"]

        # Fallback: search by pod name prefix
        cmd = f"kubectl -n {self.namespace} get pods -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        data = json.loads(out or "{}")
        for item in data.get("items", []):
            name = item["metadata"]["name"]
            if name.startswith(self.deploy) and item.get("status", {}).get("phase") == "Running":
                return item["spec"]["nodeName"]

        return None

    def _get_mongodb_pod(self) -> Optional[str]:
        svc = self.deploy.split("-", 1)[-1]
        cmd = f"kubectl -n {self.namespace} get pods -l app=mongodb,component={svc} -o jsonpath='{{.items[0].metadata.name}}'"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        pod_name = out.strip() if out else ""
        if not pod_name or pod_name.startswith("error"):
            cmd = f"kubectl -n {self.namespace} get pods -o json"
            out = self.kubectl.exec_command(cmd)
            if isinstance(out, tuple):
                out = out[0]
            data = json.loads(out or "{}")
            for item in data.get("items", []):
                name = item["metadata"]["name"]
                if name.startswith(self.deploy) and item.get("status", {}).get("phase") == "Running":
                    return name
        return pod_name if pod_name else None

    def _get_database_name(self) -> str:
        svc = self.deploy.split("-", 1)[-1]
        return f"{svc}-db"

    def mongo_write(self, hotel_id: str, lat: float, lon: float) -> bool:
        pod_name = self._get_mongodb_pod()
        if not pod_name:
            return False
        db_name = self._get_database_name()
        collection = self.deploy.split("-", 1)[-1]
        write_cmd = (
            f"kubectl -n {self.namespace} exec {pod_name} -- "
            f"mongo {db_name} --eval "
            f"'db.{collection}.insertOne({{hotelId: \"{hotel_id}\", lat: {lat}, lon: {lon}}})' "
            f"--quiet --username admin --password admin --authenticationDatabase admin"
        )
        try:
            out = self.kubectl.exec_command(write_cmd)
            fsync_cmd = (
                f"kubectl -n {self.namespace} exec {pod_name} -- "
                f"mongo {db_name} --eval 'db.runCommand({{fsync: 1}})' "
                f"--quiet --username admin --password admin --authenticationDatabase admin"
            )
            self.kubectl.exec_command(fsync_cmd)
            self.kubectl.exec_command(f"kubectl -n {self.namespace} exec {pod_name} -- sync")
            return True
        except Exception:
            return False

    def mongo_read(self, hotel_id: str) -> Optional[dict]:
        pod_name = self._get_mongodb_pod()
        if not pod_name:
            return None
        db_name = self._get_database_name()
        collection = self.deploy.split("-", 1)[-1]
        read_cmd = (
            f"kubectl -n {self.namespace} exec {pod_name} -- "
            f"mongo {db_name} --eval 'db.{collection}.findOne({{hotelId: \"{hotel_id}\"}})' "
            f"--quiet --username admin --password admin --authenticationDatabase admin"
        )
        try:
            out = self.kubectl.exec_command(read_cmd)
        except Exception:
            return None

    def _get_corruption_features(self) -> str:
        """
        Build the dm-flakey feature string based on strategy.
        Returns features like: "random_read_corrupt 500000000" or "random_read_corrupt 500000000 random_write_corrupt 500000000"
        """
        features = []
        
        if self.strategy == SilentDataCorruptionStrategy.READ_CORRUPT:
            features.append(f"random_read_corrupt {self.probability}")
        elif self.strategy == SilentDataCorruptionStrategy.WRITE_CORRUPT:
            features.append(f"random_write_corrupt {self.probability}")
        elif self.strategy == SilentDataCorruptionStrategy.BOTH_CORRUPT:
            features.append(f"random_read_corrupt {self.probability}")
            features.append(f"random_write_corrupt {self.probability}")
        
        return " ".join(features)

    @mark_fault_injected
    def inject_fault(self):
        print(f"[SDC] Starting silent data corruption injection for {self.deploy}")

        # Get target node where the deployment is running
        self.target_node = self._discover_node_for_deploy()
        if not self.target_node:
            raise RuntimeError(f"Could not find running node for deployment {self.deploy}")

        print(f"[SDC] Target node: {self.target_node}")
        print(f"[SDC] Strategy: {self.strategy}")
        print(f"[SDC] Probability: {self.probability}/1000000000 ({self.probability/10000000:.1f}%)")
        print(f"[SDC] Up interval: {self.up_interval}s, Down interval: {self.down_interval}s")

        # Get corruption features string
        features = self._get_corruption_features()
        print(f"[SDC] Features: {features}")

        # The dm-flakey device is already set up by DmFlakeyManager in Conductor
        # We just need to configure it with corruption features
        
        print(f"[SDC] Configuring dm-flakey device for corruption...")
        self.injector.dm_flakey_reload(
            self.target_node,
            DM_FLAKEY_DEVICE_NAME,
            up_interval=self.up_interval,
            down_interval=self.down_interval,
            features=features
        )

        print(f"[SDC] Triggering MongoDB write and read to exercise corruption...")
        import random
        for _ in range(10):
            test_id = "SDC_TRIGGER_"+str(random.randint(0, 10000))
            lat = 30 + random.randint(0, 10000)*0.0001
            lon = -120 + random.randint(0, 10000)*0.0001
            self.mongo_write(test_id, lat, lon)
            self.injector.drop_caches(self.target_node, show_log=False)
            self.mongo_read(test_id)

        print(f"[SDC] Silent data corruption injection complete")
        if self.up_interval == 0:
            print(f"[SDC] ⚠️  Device corruption is ALWAYS ACTIVE (no healthy intervals)")
        else:
            print(f"[SDC] Device will corrupt data for {self.down_interval}s every {self.up_interval + self.down_interval}s")

    @mark_fault_injected
    def recover_fault(self):
        print(f"[SDC] Starting recovery from silent data corruption")

        # Restore dm-flakey device to normal operation
        if hasattr(self, "target_node") and self.target_node:
            print(f"[SDC] Restoring dm-flakey device to normal operation on {self.target_node}")
            self.injector.dm_flakey_reload(
                self.target_node,
                DM_FLAKEY_DEVICE_NAME,
                up_interval=1,
                down_interval=0,
                features=""
            )
            print(f"[SDC] ✅ dm-flakey device restored to normal operation")
        
        # Clean up and redeploy the app
        self.app.cleanup()
        
        try:
            cleanup_pods = self.kubectl.exec_command(
                "kubectl get pods -n openebs --no-headers | grep 'cleanup-pvc-' | awk '{print $1}'"
            ).strip()
            if cleanup_pods:
                pod_list = [p for p in cleanup_pods.splitlines() if p.strip()]
                for pod in pod_list:
                    # Delete failed cleanup pods
                    self.kubectl.exec_command(f"kubectl delete pod -n openebs {pod} --ignore-not-found")
                print(f"[SDC] Cleaned up {len(pod_list)} OpenEBS cleanup pod(s)")
        except Exception as e:
            print(f"[SDC] ⚠️  Warning: Failed to clean up OpenEBS cleanup pods: {e}")
        
        self.dm_flakey_manager.setup_openebs_dm_flakey_infrastructure() # This helps clean up any corrupted data on the affected storage directories
        self.app.deploy()
        self.app.start_workload()
        
        print(f"[SDC] ✅ Recovery complete - App restarted with clean state")
