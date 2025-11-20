from enum import StrEnum
from typing import Dict, Optional, Tuple

from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.latent_sector_error_localization_oracle import LatentSectorErrorLocalizationOracle
from sregym.conductor.oracles.workload import WorkloadOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_kernel import KernelInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.service.dm_dust_manager import DM_DUST_DEVICE_NAME
from sregym.utils.decorators import mark_fault_injected

# Constants
DEFAULT_TARGET_DEPLOY = "mongodb-geo"
DEFAULT_NAMESPACE = "hotel-reservation"
DEFAULT_BAD_BLOCK_STEP = 1000
TEST_BAD_BLOCKS = [100, 200, 300]


class LatentSectorErrorStrategy(StrEnum):
    """Strategy for injecting bad blocks in dm-dust device."""

    TEST = "test"
    EVERY_1000 = "every_1000"  # Also test strategy
    TARGETED = "targeted"


class LatentSectorError(Problem):
    """
    Simulates latent sector errors (LSE) on a MongoDB PVC
    (geo, profile, reservation, etc.) using dm-dust inside Khaos.
    """

    def __init__(
        self,
        target_deploy: str = DEFAULT_TARGET_DEPLOY,
        namespace: str = DEFAULT_NAMESPACE,
        strategy: LatentSectorErrorStrategy = LatentSectorErrorStrategy.TARGETED,
    ):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = namespace
        self.deploy = target_deploy
        self.injector = KernelInjector(self.kubectl)
        self.target_node: Optional[str] = None
        self.pvc_path: Optional[str] = None
        self.strategy = strategy

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.localization_oracle = LatentSectorErrorLocalizationOracle(
            problem=self, namespace=self.namespace, expected_deployment_name=self.deploy
        )

        self.app.create_workload()

    def requires_khaos(self) -> bool:
        """This problem requires Khaos for dm-dust infrastructure setup."""
        return True

    def _get_kubectl_json(self, command: str) -> dict:
        """Execute kubectl command and parse JSON output."""
        out = self.kubectl.exec_command(command)
        if not out:
            raise RuntimeError(f"Command returned empty output: {command}")
        import json

        return json.loads(out)

    def _discover_node_for_deploy(self) -> Optional[str]:
        """Return the node where the target deployment is running."""
        # First try with a label selector (common OpenEBS hotel-reservation pattern)
        svc = self.deploy.split("-", 1)[-1]  # e.g. "geo"
        cmd = f"kubectl -n {self.namespace} get pods -l app=mongodb,component={svc} -o json"
        try:
            data = self._get_kubectl_json(cmd)
            for item in data.get("items", []):
                if item.get("status", {}).get("phase") == "Running":
                    return item["spec"]["nodeName"]
        except (KeyError, RuntimeError):
            pass

        # Fallback: search by pod name prefix
        cmd = f"kubectl -n {self.namespace} get pods -o json"
        try:
            data = self._get_kubectl_json(cmd)
            for item in data.get("items", []):
                name = item["metadata"]["name"]
                if name.startswith(self.deploy) and item.get("status", {}).get("phase") == "Running":
                    return item["spec"]["nodeName"]
        except (KeyError, RuntimeError):
            pass

        return None

    def _discover_pvc(self) -> Tuple[str, str, str]:
        """
        Discover PVC information for the target deployment.

        Returns:
            Tuple of (pvc_name, pv_name, local_path)
        """
        cmd = f"kubectl -n {self.namespace} get pvc -o json"
        data = self._get_kubectl_json(cmd)

        pvc_name, pv_name = None, None
        deploy_component = self.deploy.split("-")[-1]  # e.g. "geo"

        for item in data.get("items", []):
            claim = item["metadata"]["name"]
            if deploy_component in claim:  # match geo, profile, etc.
                pvc_name = claim
                pv_name = item["spec"]["volumeName"]
                break

        if not pvc_name:
            raise RuntimeError(f"Could not find PVC for deployment {self.deploy}")

        cmd = f"kubectl get pv {pv_name} -o json"
        pv = self._get_kubectl_json(cmd)

        try:
            local_path = pv["spec"]["local"]["path"]
        except KeyError:
            raise RuntimeError(f"PV {pv_name} does not have a local path (not a local PV)")

        return pvc_name, pv_name, local_path

    def _get_openebs_storage_size(self, node: str) -> Dict[str, int]:
        """
        Get storage information for the OpenEBS dm-dust device.

        Returns:
            Dictionary with sectors, size_bytes, size_mb, size_gb, block_size
        """
        script = f"""
set -e
DM_NAME={DM_DUST_DEVICE_NAME}
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
        try:
            sectors, size_bytes, size_mb, size_gb, block_size = result.split(",")
            return {
                "sectors": int(sectors),
                "size_bytes": int(size_bytes),
                "size_mb": int(size_mb),
                "size_gb": int(size_gb),
                "block_size": int(block_size),
            }
        except (ValueError, IndexError) as e:
            raise RuntimeError(f"Failed to parse storage size output: {result}") from e

    def _get_target_file_blocks(self, node: str) -> list[int]:
        """
        Identify physical blocks used by MongoDB data files (.wt) on the target node.
        Returns a list of block numbers (in 512b sectors) to corrupt.
        """
        # Find mount point of the dm-dust device
        cmd = f"lsblk -o MOUNTPOINT -n /dev/mapper/{DM_DUST_DEVICE_NAME}"
        mount_point = self.injector._exec_on_node(node, cmd).strip()

        if not mount_point:
            print(f"[MongoDBLSE] Warning: {DM_DUST_DEVICE_NAME} is not mounted. Cannot find target files.")
            return []

        print(f"[MongoDBLSE] Found mount point: {mount_point}")

        # Script to find blocks
        script = f"""
        set -e
        FILES=$(find {mount_point} -name "*.wt")
        BAD_BLOCKS=""
        for FILE in $FILES; do
            BS=$(stat -f -c %S "$FILE")
            # Get start of each extent
            # filefrag -v output format:
            # ext: logical_offset: physical_offset: length: ...
            # 0: 0.. 0: 34048.. 34048: 1:
            OFFSETS=$(filefrag -v "$FILE" | awk '/^[ ]*[0-9]+:/ {{print $4}}' | cut -d. -f1)
            for OFF in $OFFSETS; do
                START_SECTOR=$((OFF * BS / 512))
                # Corrupt 10 sectors (5KB) at the start of each extent to ensure we hit it
                for I in $(seq 0 9); do
                    BAD_BLOCKS="$BAD_BLOCKS $((START_SECTOR + I))"
                done
            done
        done
        echo $BAD_BLOCKS
        """

        result = self.injector._exec_on_node(node, script).strip()
        try:
            return [int(b) for b in result.split()]
        except ValueError:
            return []

    def _inject_badblocks_by_strategy(self, node: str, storage_info: Dict[str, int]) -> None:
        """Inject bad blocks according to the configured strategy."""
        if self.strategy == LatentSectorErrorStrategy.EVERY_1000:
            start_sector = 0
            end_sector = storage_info["sectors"]
            step = DEFAULT_BAD_BLOCK_STEP

            self.injector.dm_dust_add_badblocks_range(
                node, DM_DUST_DEVICE_NAME, start=start_sector, end=end_sector, step=step
            )

        elif self.strategy == LatentSectorErrorStrategy.TEST:
            self.injector.dm_dust_add_badblocks(node, DM_DUST_DEVICE_NAME, TEST_BAD_BLOCKS)

        elif self.strategy == LatentSectorErrorStrategy.TARGETED:
            print(f"[MongoDBLSE] Strategy TARGETED: Identifying MongoDB data blocks...")
            blocks = self._get_target_file_blocks(node)
            if not blocks:
                print(f"[MongoDBLSE] Warning: No target blocks found. Falling back to TEST strategy.")
                self.injector.dm_dust_add_badblocks(node, DM_DUST_DEVICE_NAME, TEST_BAD_BLOCKS)
            else:
                print(f"[MongoDBLSE] Injecting {len(blocks)} bad blocks targeting data files.")
                # Inject in chunks to avoid command line length limits
                chunk_size = 1000
                for i in range(0, len(blocks), chunk_size):
                    chunk = blocks[i : i + chunk_size]
                    self.injector.dm_dust_add_badblocks(node, DM_DUST_DEVICE_NAME, chunk)

        else:
            raise ValueError(f"Unknown strategy: {self.strategy}")

    @mark_fault_injected
    def inject_fault(self):
        """Inject latent sector errors using dm-dust bad blocks."""
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
        self.injector.dm_dust_clear(self.target_node, DM_DUST_DEVICE_NAME)

        # Ensure we start in bypass mode
        print(f"[MongoDBLSE] Setting device to bypass mode...")
        self.injector.dm_dust_disable(self.target_node, DM_DUST_DEVICE_NAME)

        # Apply strategy-based bad blocks injection
        storage_info = self._get_openebs_storage_size(self.target_node)
        if storage_info["sectors"] == 0:
            raise RuntimeError(
                f"OpenEBS dm-dust device not found or has 0 sectors on node {self.target_node}. "
                f"Ensure dm-dust infrastructure is set up."
            )

        self._inject_badblocks_by_strategy(self.target_node, storage_info)

        print(f"[MongoDBLSE] Enabling bad block simulation (fail_read_on_bad_block mode)")
        self.injector.dm_dust_enable(self.target_node, DM_DUST_DEVICE_NAME)

        # Drop caches to force disk reads
        print(f"[MongoDBLSE] Dropping caches to force disk reads...")
        self.injector.drop_caches(self.target_node)

        print(f"[MongoDBLSE] Latent sector error injection complete")

    def _restart_mongodb_pod(self) -> None:
        """Restart the MongoDB deployment to recover from CrashLoopBackOff."""
        print(f"[MongoDBLSE] Restarting MongoDB deployment {self.deploy}...")
        cmd = f"kubectl -n {self.namespace} rollout restart deployment {self.deploy}"
        self.kubectl.exec_command(cmd)
        print(f"[MongoDBLSE] ✅ Deployment restart initiated")

    @mark_fault_injected
    def recover_fault(self):
        """Recover from latent sector error injection by clearing bad blocks."""
        print(f"[MongoDBLSE] Starting recovery from latent sector error injection")

        if not self.target_node:
            print(f"[MongoDBLSE] No target node found, skipping recovery")
            return

        print(f"[MongoDBLSE] Disabling bad block simulation on {self.target_node}")
        self.injector.dm_dust_disable(self.target_node, DM_DUST_DEVICE_NAME)

        print(f"[MongoDBLSE] Clearing all bad blocks...")
        self.injector.dm_dust_clear(self.target_node, DM_DUST_DEVICE_NAME)

        # Verify cleanup
        blocks = self.injector.dm_dust_list(self.target_node, DM_DUST_DEVICE_NAME)
        if blocks != "No blocks in badblocklist":
            print(f"[MongoDBLSE] Warning: Bad blocks still present: {blocks}")
        else:
            print(f"[MongoDBLSE] ✅ All bad blocks cleared")

        # Restart MongoDB pod to recover instantly
        self._restart_mongodb_pod()

        print(f"[MongoDBLSE] Recovery complete")
