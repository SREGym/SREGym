import json
import shlex
import subprocess
import time
from typing import Dict, List, Optional

from sregym.service.kubectl import KubeCtl

# Constants
DEFAULT_KHAOS_NS = "khaos"
DEFAULT_KHAOS_LABEL = "app=khaos"
DM_FLAKEY_DEVICE_NAME = "openebs_flakey"
DM_FLAKEY_BACKING_FILE = "/var/tmp/openebs_dm_flakey.img"
DM_FLAKEY_BACKING_FILE_SIZE_GB = 5
OPENEBS_LOCAL_PATH = "/var/openebs/local"
DEFAULT_BLOCK_SIZE = 512
SETUP_TIMEOUT_SECONDS = 120


class DmFlakeyManager:
    """
    Manages dm-flakey infrastructure setup for fault injection.

    This class sets up dm-flakey devices to intercept all OpenEBS local storage,
    allowing any application using OpenEBS to have fault injection capabilities
    without needing to know specific service names or PVC details.

    The setup process:
    1. Creates a large dm-flakey device
    2. Mounts it at /var/openebs/local
    3. All PVs created by OpenEBS will automatically use this dm-flakey device
    """

    def __init__(
        self,
        kubectl: KubeCtl,
        khaos_ns: str = DEFAULT_KHAOS_NS,
        khaos_label: str = DEFAULT_KHAOS_LABEL,
    ):
        self.kubectl = kubectl
        self.khaos_ns = khaos_ns
        self.khaos_label = khaos_label
        self._pod_cache: Dict[str, str] = {}  # Cache pod names by node

    def setup_openebs_dm_flakey_infrastructure(self, nodes: Optional[List[str]] = None) -> None:
        """
        Set up dm-flakey to intercept all OpenEBS local storage on the specified nodes.
        Creates a dm-flakey device that will be used for all PVs created in /var/openebs/local/.

        Args:
            nodes: List of node names to set up. If None, sets up on all nodes in the cluster.
        """
        if nodes is None:
            nodes_response = self.kubectl.list_nodes()
            nodes = [node.metadata.name for node in nodes_response.items]

        if not nodes:
            raise RuntimeError("No nodes available for dm-flakey setup")

        for node in nodes:
            try:
                self._setup_dm_flakey_on_node(node)
                print(f"[dm-flakey] ✅ Set up dm-flakey infrastructure on {node}")
            except Exception as e:
                print(f"[dm-flakey] ❌ Failed to set up dm-flakey on {node}: {e}")
                raise

    def _setup_dm_flakey_on_node(self, node: str) -> None:
        """Set up dm-flakey device to intercept OpenEBS storage on a single node."""
        print(f"[dm-flakey] Setting up dm-flakey on {node}...")

        # Build the complete setup script from logical sections
        script_parts = [
            self._build_module_check_script(),
            self._build_cleanup_script(),
            self._build_backing_file_script(),
            self._build_dm_flakey_create_script(),
            self._build_mount_script(),
        ]

        full_script = "set -e\n" + "\n".join(script_parts)

        # Execute using nsenter to access host namespace
        pod = self._get_khaos_pod_on_node(node)
        cmd = [
            "kubectl",
            "-n",
            self.khaos_ns,
            "exec",
            pod,
            "--",
            "nsenter",
            "-t",
            "1",
            "-m",
            "-u",
            "-i",
            "-n",
            "-p",
            "sh",
            "-c",
            full_script,
        ]

        try:
            rc = subprocess.run(cmd, timeout=SETUP_TIMEOUT_SECONDS, capture_output=True, text=True)
            if rc.returncode != 0:
                error_msg = f"Failed to setup dm-flakey on {node}: return code {rc.returncode}"
                if rc.stderr:
                    error_msg += f"\nStderr: {rc.stderr}"
                if rc.stdout:
                    error_msg += f"\nStdout: {rc.stdout}"
                raise RuntimeError(error_msg)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Timeout setting up dm-flakey on {node} after {SETUP_TIMEOUT_SECONDS} seconds")

    def _build_module_check_script(self) -> str:
        """Build script to check and load dm_flakey module."""
        return f"""
echo 'Setting up dm-flakey for OpenEBS local storage...'
echo 'Checking dm_flakey module...'
modprobe dm_flakey || {{ echo 'Failed to load dm_flakey module'; exit 1; }}
lsmod | grep dm_flakey || {{ echo 'dm_flakey module not found in lsmod'; exit 1; }}
echo 'Checking device-mapper targets...'
dmsetup targets | grep flakey || {{ echo 'flakey target not available in dmsetup'; exit 1; }}
"""

    def _build_cleanup_script(self) -> str:
        """Build script to clean up existing dm-flakey infrastructure."""
        openebs_path = OPENEBS_LOCAL_PATH
        return f"""
DM_NAME={DM_FLAKEY_DEVICE_NAME}
BACKING_FILE={shlex.quote(DM_FLAKEY_BACKING_FILE)}

echo 'Cleaning up any existing dm-flakey infrastructure...'

# Unmount if mounted
if mountpoint -q {shlex.quote(openebs_path)} 2>/dev/null; then
    echo 'Unmounting {openebs_path}...'
    umount {shlex.quote(openebs_path)} 2>/dev/null || umount -f {shlex.quote(openebs_path)} 2>/dev/null || true
    sleep 1
fi

# Remove existing dm device
if dmsetup info $DM_NAME >/dev/null 2>&1; then
    echo 'Found existing device $DM_NAME, attempting removal...'
    mount | grep "/dev/mapper/$DM_NAME" | awk '{{print $3}}' | xargs -r -I {{}} umount -l {{}} 2>/dev/null || true
    sleep 1
    if dmsetup remove $DM_NAME 2>/dev/null; then
        echo 'Device removed successfully'
    elif dmsetup remove --force $DM_NAME 2>/dev/null; then
        echo 'Device removed with --force'
    else
        echo 'Device is busy, renaming and marking for deferred removal...'
        timestamp=$(date +%s)
        dmsetup rename $DM_NAME ${{DM_NAME}}_old_${{timestamp}} 2>/dev/null || true
        dmsetup remove --deferred ${{DM_NAME}}_old_${{timestamp}} 2>/dev/null || true
        echo 'Old device will be cleaned up automatically when kernel releases it'
    fi
fi

# Clean up backing file and loop devices
if [ -f $BACKING_FILE ]; then
    echo 'Cleaning up old backing file and loop devices...'
    losetup -j $BACKING_FILE 2>/dev/null | awk -F: '{{print $1}}' | xargs -r losetup -d 2>/dev/null || true
    rm -f $BACKING_FILE
fi
"""

    def _build_backing_file_script(self) -> str:
        """Build script to create backing file and loop device."""
        openebs_path = OPENEBS_LOCAL_PATH
        return f"""
BACKING_FILE={shlex.quote(DM_FLAKEY_BACKING_FILE)}

echo 'Preparing OpenEBS directory at {openebs_path}...'
rm -rf {shlex.quote(openebs_path)}/* 2>/dev/null || true
mkdir -p {shlex.quote(openebs_path)}

echo 'Creating {DM_FLAKEY_BACKING_FILE_SIZE_GB}GB backing file for OpenEBS dm-flakey...'
dd if=/dev/zero of=$BACKING_FILE bs=1M count={DM_FLAKEY_BACKING_FILE_SIZE_GB * 1024}

echo 'Setting up loop device...'
LOOP_DEV=$(losetup -f --show $BACKING_FILE)
echo "Loop device: $LOOP_DEV"
"""

    def _build_dm_flakey_create_script(self) -> str:
        """Build script to create and format dm-flakey device."""
        return f"""
DM_NAME={DM_FLAKEY_DEVICE_NAME}
SECTORS=$(blockdev --getsz $LOOP_DEV)
echo "Sectors: $SECTORS"

echo 'Creating healthy dm-flakey device for OpenEBS...'
echo 'Running dmsetup create command...'
# up=1, down=0 means always up (pass-through)
dmsetup create $DM_NAME --table "0 $SECTORS flakey $LOOP_DEV 0 1 0" || {{
    echo 'dmsetup create failed'
    dmsetup targets
    exit 1
}}

echo 'dmsetup create completed successfully'
echo 'Verifying dm device was created...'
ls -la /dev/mapper/$DM_NAME || {{ echo 'dm device not found'; exit 1; }}

echo 'Formatting dm-flakey device with ext4...'
mkfs.ext4 -F /dev/mapper/$DM_NAME || {{ echo 'mkfs.ext4 failed'; exit 1; }}
"""

    def _build_mount_script(self) -> str:
        """Build script to mount dm-flakey device and set permissions."""
        openebs_path = OPENEBS_LOCAL_PATH
        return f"""
DM_NAME={DM_FLAKEY_DEVICE_NAME}
echo 'Mounting dm-flakey device at {openebs_path}...'
mount /dev/mapper/$DM_NAME {shlex.quote(openebs_path)}

echo 'Setting proper permissions...'
chmod 755 {shlex.quote(openebs_path)}

echo 'OpenEBS dm-flakey infrastructure ready - all PVs will use dm-flakey'
"""

    def _get_khaos_pod_on_node(self, node: str) -> str:
        """Find a running Khaos pod on the specified node, with caching."""
        if node in self._pod_cache:
            return self._pod_cache[node]

        cmd = f"kubectl -n {shlex.quote(self.khaos_ns)} get pods -l {shlex.quote(self.khaos_label)} -o json"
        out = self.kubectl.exec_command(cmd)
        if not out:
            raise RuntimeError(f"Failed to get pods: empty response")

        data = json.loads(out)
        for item in data.get("items", []):
            if item.get("spec", {}).get("nodeName") == node and item.get("status", {}).get("phase") == "Running":
                pod_name = item["metadata"]["name"]
                self._pod_cache[node] = pod_name
                return pod_name

        raise RuntimeError(f"No running Khaos pod found on node {node}")
