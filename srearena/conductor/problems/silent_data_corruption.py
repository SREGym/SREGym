from enum import StrEnum
import json

from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_kernel import KernelInjector
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.service.dm_flakey_manager import DM_FLAKEY_DEVICE_NAME
from srearena.conductor.oracles.workload import WorkloadOracle


class SilentDataCorruptionStrategy(StrEnum):
    READ_CORRUPT = "read_corrupt"
    WRITE_CORRUPT = "write_corrupt"
    BOTH_CORRUPT = "both_corrupt"


class SilentDataCorruption(Problem):

    DM_FLAKEY_DEVICE_NAME = DM_FLAKEY_DEVICE_NAME

    def __init__(
        self,
        target_deploy: str = "mongodb-geo",
        namespace: str = "hotel-reservation",
        strategy: SilentDataCorruptionStrategy = SilentDataCorruptionStrategy.BOTH_CORRUPT,
        probability: int = 500000000,  # 50% probability (0-1000000000 scale)
        up_interval: int = 0,  # Seconds device is healthy (0 = never healthy)
        down_interval: int = 999999,  # Seconds device corrupts data (large = always corrupting)
    ):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = namespace
        self.deploy = target_deploy
        self.injector = KernelInjector(self.kubectl)
        self.target_node = None
        self.strategy = strategy
        self.probability = probability
        self.up_interval = up_interval
        self.down_interval = down_interval

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.app.create_workload()
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.deploy])
        self.mitigation_oracle = CompoundedOracle(
            self,
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
        )

    def _discover_node_for_deploy(self) -> str:
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

        # Use dm-flakey infrastructure to inject corruption
        # The dm-flakey device is already set up by DmFlakeyManager in Conductor
        # We just need to configure it with corruption features
        
        print(f"[SDC] Configuring dm-flakey device for corruption...")
        self.injector.dm_flakey_reload(
            self.target_node,
            self.DM_FLAKEY_DEVICE_NAME,
            up_interval=self.up_interval,
            down_interval=self.down_interval,
            features=features
        )

        print(f"[SDC] Silent data corruption injection complete")
        if self.up_interval == 0:
            print(f"[SDC] ⚠️  Device corruption is ALWAYS ACTIVE (no healthy intervals)")
        else:
            print(f"[SDC] Device will corrupt data for {self.down_interval}s every {self.up_interval + self.down_interval}s")

    @mark_fault_injected
    def recover_fault(self):
        print(f"[SDC] Starting recovery from silent data corruption")

        if hasattr(self, "target_node") and self.target_node:
            print(f"[SDC] Restoring dm-flakey device to normal operation on {self.target_node}")
            
            # Reload dm-flakey without corruption features (just passthrough)
            self.injector.dm_flakey_reload(
                self.target_node,
                self.DM_FLAKEY_DEVICE_NAME,
                up_interval=999999,  # Effectively always up
                down_interval=0,     # Never down
                features=""          # No corruption
            )
            
            print(f"[SDC] ✅ dm-flakey device restored to normal operation")
        else:
            print(f"[SDC] No target node found, skipping recovery")

        print(f"[SDC] Recovery complete")

