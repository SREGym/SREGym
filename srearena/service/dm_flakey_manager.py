import shlex
import subprocess

from srearena.service.kubectl import KubeCtl

DM_FLAKEY_DEVICE_NAME = "openebs_flakey"
DM_FLAKEY_BACKING_FILE = "/var/tmp/openebs_dm_flakey.img"
DM_FLAKEY_BACKING_FILE_SIZE_GB = 5


class DmFlakeyManager:
    """
    Manages dm-flakey infrastructure setup for fault injection.

    This class sets up dm-flakey devices to intercept all OpenEBS local storage,
    allowing any application using OpenEBS to have silent data corruption and
    intermittent failure capabilities without needing to know specific service
    names or PVC details.
    """

    def __init__(self, kubectl: KubeCtl, khaos_ns: str = "khaos", khaos_label: str = "app=khaos"):
        self.kubectl = kubectl
        self.khaos_ns = khaos_ns
        self.khaos_label = khaos_label

    def setup_openebs_dm_flakey_infrastructure(self, nodes: list[str] = None):
        """
        Set up dm-flakey to intercept all OpenEBS local storage on the specified nodes.
        Creates a dm-flakey device that will be used for all PVs created in /var/openebs/local/.

        This works by:
        1. Creating a large dm-flakey device
        2. Mounting it at /var/openebs/local
        3. All PVs created by OpenEBS will automatically use this dm-flakey device
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

    def _setup_dm_flakey_on_node(self, node: str):
        """Set up dm-flakey device to intercept OpenEBS storage on a single node."""
        openebs_path = "/var/openebs/local"
        pod = self._get_khaos_pod_on_node(node)

        inner_cmd = (
            "set -e; "
            "echo 'Setting up dm-flakey for OpenEBS local storage...'; "
            "echo 'Checking dm_flakey module...'; "
            "modprobe dm_flakey || { echo 'Failed to load dm_flakey module'; exit 1; }; "
            "lsmod | grep dm_flakey || { echo 'dm_flakey module not found in lsmod'; exit 1; }; "
            "echo 'Checking device-mapper targets...'; "
            "dmsetup targets | grep flakey || { echo 'flakey target not available in dmsetup'; exit 1; }; "
            f"DM_NAME={DM_FLAKEY_DEVICE_NAME}; "
            f"BACKING_FILE={DM_FLAKEY_BACKING_FILE}; "
            "echo 'Cleaning up any existing dm-flakey infrastructure...'; "
            f"if mountpoint -q {shlex.quote(openebs_path)} 2>/dev/null; then "
            f"  echo 'Unmounting {openebs_path}...'; "
            f"  umount {shlex.quote(openebs_path)} 2>/dev/null || umount -f {shlex.quote(openebs_path)} 2>/dev/null || true; "
            "  sleep 1; "
            "fi; "
            "if dmsetup info $DM_NAME >/dev/null 2>&1; then "
            "  echo 'Found existing device $DM_NAME, attempting removal...'; "
            "  mount | grep \"/dev/mapper/$DM_NAME\" | awk '{print $3}' | xargs -r -I {} umount -l {} 2>/dev/null || true; "
            "  sleep 1; "
            "  if dmsetup remove $DM_NAME 2>/dev/null; then "
            "    echo 'Device removed successfully'; "
            "  elif dmsetup remove --force $DM_NAME 2>/dev/null; then "
            "    echo 'Device removed with --force'; "
            "  else "
            "    echo 'Device is busy, renaming and marking for deferred removal...'; "
            "    timestamp=$(date +%s); "
            "    dmsetup rename $DM_NAME ${DM_NAME}_old_${timestamp} 2>/dev/null || true; "
            "    dmsetup remove --deferred ${DM_NAME}_old_${timestamp} 2>/dev/null || true; "
            "    echo 'Old device will be cleaned up automatically when kernel releases it'; "
            "  fi; "
            "fi; "
            "if [ -f $BACKING_FILE ]; then "
            "  echo 'Cleaning up old backing file and loop devices...'; "
            "  losetup -j $BACKING_FILE 2>/dev/null | awk -F: '{print $1}' | xargs -r losetup -d 2>/dev/null || true; "
            "  rm -f $BACKING_FILE; "
            "fi; "
            f"echo 'Preparing OpenEBS directory at {openebs_path}...'; "
            f"rm -rf {shlex.quote(openebs_path)}/* 2>/dev/null || true; "
            f"mkdir -p {shlex.quote(openebs_path)}; "
            f"echo 'Creating {DM_FLAKEY_BACKING_FILE_SIZE_GB}GB backing file for OpenEBS dm-flakey...'; "
            f"dd if=/dev/zero of=$BACKING_FILE bs=1M count={DM_FLAKEY_BACKING_FILE_SIZE_GB * 1024}; "
            "echo 'Setting up loop device...'; "
            "LOOP_DEV=$(losetup -f --show $BACKING_FILE); "
            'echo "Loop device: $LOOP_DEV"; '
            "SECTORS=$(blockdev --getsz $LOOP_DEV); "
            'echo "Sectors: $SECTORS"; '
            "echo 'Creating healthy dm-flakey device for OpenEBS (default: always up, no corruption)...'; "
            "echo 'Running dmsetup create command...'; "
            # Start with a safe default: always up (999999s), never down (0s), no features
            "dmsetup create $DM_NAME --table \"0 $SECTORS flakey $LOOP_DEV 0 999999 0\" || { echo 'dmsetup create failed'; dmsetup targets; exit 1; }; "
            "echo 'dmsetup create completed successfully'; "
            "echo 'Verifying dm device was created...'; "
            "ls -la /dev/mapper/$DM_NAME || { echo 'dm device not found'; exit 1; }; "
            "echo 'Formatting dm-flakey device with ext4...'; "
            "mkfs.ext4 -F /dev/mapper/$DM_NAME || { echo 'mkfs.ext4 failed'; exit 1; }; "
            f"echo 'Mounting dm-flakey device at {openebs_path}...'; "
            f"mount /dev/mapper/$DM_NAME {shlex.quote(openebs_path)}; "
            "echo 'Setting proper permissions...'; "
            f"chmod 755 {shlex.quote(openebs_path)}; "
            "echo 'OpenEBS dm-flakey infrastructure ready - all PVs will use dm-flakey'"
        )

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
            inner_cmd,
        ]

        print(f"[dm-flakey] Setting up dm-flakey on {node}...")
        try:
            rc = subprocess.run(cmd, timeout=120)
            if rc.returncode != 0:
                raise RuntimeError(f"Failed to setup dm-flakey on {node}: return code {rc.returncode}")
        except subprocess.TimeoutExpired:
            print(f"[dm-flakey DEBUG] Command timed out on {node} after 120 seconds")
            raise RuntimeError(f"Timeout setting up dm-flakey on {node} after 120 seconds")

    def _get_khaos_pod_on_node(self, node: str) -> str:
        """Find a running Khaos pod on the specified node."""
        cmd = f"kubectl -n {shlex.quote(self.khaos_ns)} get pods -l {shlex.quote(self.khaos_label)} -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]

        import json

        data = json.loads(out or "{}")
        for item in data.get("items", []):
            if item.get("spec", {}).get("nodeName") == node and item.get("status", {}).get("phase") == "Running":
                return item["metadata"]["name"]

        raise RuntimeError(f"No running Khaos pod found on node {node}")

