from enum import StrEnum
import json

from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_kernel import KernelInjector
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.service.dm_dust_manager import DM_DUST_DEVICE_NAME


class LatentSectorErrorStrategy(StrEnum):
    TEST = "test"
    EVERY_1000 = "every_1000" # Also test strategy

class LatentSectorError(Problem):
    """
    Simulates latent sector errors (LSE) on a MongoDB PVC
    (geo, profile, reservation, etc.) using dm-dust inside Khaos.
    """

    DM_DUST_DEVICE_NAME = DM_DUST_DEVICE_NAME

    def __init__(self, target_deploy: str = "mongodb-geo", namespace: str = "hotel-reservation", strategy: LatentSectorErrorStrategy = LatentSectorErrorStrategy.EVERY_1000):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = namespace
        self.deploy = target_deploy
        self.injector = KernelInjector(self.kubectl)
        self.target_node = None
        self.pvc_path = None
        self.strategy = strategy

        # TODO: We should throw an error if on an emulated cluster, this will only work on real linux nodes
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.app.create_workload()
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.deploy])

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

    def _discover_pvc(self) -> tuple[str, str, str]:
        """
        Return (pvc_name, pv_name, local_path)
        """
        cmd = f"kubectl -n {self.namespace} get pvc -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        data = json.loads(out or "{}")

        pvc_name, pv_name = None, None
        for item in data.get("items", []):
            claim = item["metadata"]["name"]
            if self.deploy.split("-")[-1] in claim:  # match geo, profile, etc.
                pvc_name = claim
                pv_name = item["spec"]["volumeName"]
                break

        if not pvc_name:
            raise RuntimeError(f"Could not find PVC for deploy {self.deploy}")

        out = self.kubectl.exec_command(f"kubectl get pv {pv_name} -o json")
        if isinstance(out, tuple):
            out = out[0]
        pv = json.loads(out or "{}")
        local_path = pv["spec"]["local"]["path"]

        return pvc_name, pv_name, local_path

    def _get_openebs_storage_size(self, node: str) -> dict:

        script = """
        set -e
        DM_NAME=openebs_dust
        if [ -e /dev/mapper/$DM_NAME ]; then
            SECTORS=$(blockdev --getsz /dev/mapper/$DM_NAME)
            SIZE_BYTES=$(blockdev --getsize64 /dev/mapper/$DM_NAME)
            SIZE_MB=$((SIZE_BYTES / 1024 / 1024))
            SIZE_GB=$((SIZE_BYTES / 1024 / 1024 / 1024))
            BLOCK_SIZE=$(blockdev --getbsz /dev/mapper/$DM_NAME)
            echo "$SECTORS,$SIZE_BYTES,$SIZE_MB,$SIZE_GB,$BLOCK_SIZE"
        else
            echo "0,0,0,0,0"
        fi
        """
        
        result = self.injector._exec_on_node(node, script).strip()
        sectors, size_bytes, size_mb, size_gb, block_size = result.split(',')
        
        return {
            'sectors': int(sectors),
            'size_bytes': int(size_bytes),
            'size_mb': int(size_mb),
            'size_gb': int(size_gb),
            'block_size': int(block_size)
        }

    def _inject_badblocks_by_strategy(self, node: str, storage_info: dict):
     
        if self.strategy == LatentSectorErrorStrategy.EVERY_1000:
            # Every 1000 sectors

            start_sector = 0
            end_sector = storage_info['sectors']
            step = 1000
            
            self.injector.dm_dust_add_badblocks_range(node, self.DM_DUST_DEVICE_NAME, start=start_sector, end=end_sector, step=step)

        elif self.strategy == LatentSectorErrorStrategy.TEST:
            # Some specific blocks 100, 200, 300

            self.injector.dm_dust_add_badblocks(node, self.DM_DUST_DEVICE_NAME, [100, 200, 300])

    @mark_fault_injected
    def inject_fault(self):
        print(f"[MongoDBLSE] Starting latent sector error injection for {self.deploy}")

        # Get target node where the deployment is running
        self.target_node = self._discover_node_for_deploy()
        if not self.target_node:
            raise RuntimeError(f"Could not find running node for deployment {self.deploy}")

        print(f"[MongoDBLSE] Target node: {self.target_node}")

        # Since dm-dust infrastructure is already set up by Conductor,
        # we just need to add bad blocks and enable them
        
        # Clear any existing bad blocks from previous runs
        print(f"[MongoDBLSE] Clearing existing bad blocks...")
        self.injector.dm_dust_clear(self.target_node, self.DM_DUST_DEVICE_NAME)
        
        # Ensure we start in bypass mode
        print(f"[MongoDBLSE] Setting device to bypass mode...")
        self.injector.dm_dust_disable(self.target_node, self.DM_DUST_DEVICE_NAME)

        # Apply strategy-based bad blocks injection
        storage_info = self._get_openebs_storage_size(self.target_node)
        self._inject_badblocks_by_strategy(self.target_node, storage_info)

        print(f"[MongoDBLSE] Enabling bad block simulation (fail_read_on_bad_block mode)")
        self.injector.dm_dust_enable(self.target_node, self.DM_DUST_DEVICE_NAME)

        print(f"[MongoDBLSE] Latent sector error injection complete")

    @mark_fault_injected
    def recover_fault(self):
        print(f"[MongoDBLSE] Starting recovery from latent sector error injection")

        if hasattr(self, "target_node") and self.target_node:
            print(f"[MongoDBLSE] Disabling bad block simulation on {self.target_node}")
            self.injector.dm_dust_disable(self.target_node, self.DM_DUST_DEVICE_NAME)
            
            print(f"[MongoDBLSE] Clearing all bad blocks...")
            self.injector.dm_dust_clear(self.target_node, self.DM_DUST_DEVICE_NAME)
            
            # Verify cleanup
            blocks = self.injector.dm_dust_list(self.target_node, self.DM_DUST_DEVICE_NAME)
            if blocks != "No blocks in badblocklist":
                print(f"[MongoDBLSE] Warning: Bad blocks still present: {blocks}")
            else:
                print(f"[MongoDBLSE] ✅ All bad blocks cleared")
        else:
            print(f"[MongoDBLSE] No target node found, skipping recovery")

        print(f"[MongoDBLSE] Recovery complete")
