import shlex
import subprocess

from srearena.service.kubectl import KubeCtl


class DmDustManager:
    """
    Manages dm-dust infrastructure setup for fault injection.

    This class sets up dm-dust devices to intercept all OpenEBS local storage,
    allowing any application using OpenEBS to have fault injection capabilities
    without needing to know specific service names or PVC details.
    """

    def __init__(self, kubectl: KubeCtl, khaos_ns: str = "khaos", khaos_label: str = "app=khaos"):
        self.kubectl = kubectl
        self.khaos_ns = khaos_ns
        self.khaos_label = khaos_label

    def setup_openebs_dm_dust_infrastructure(self, nodes: list[str] = None):
        """
        Set up dm-dust to intercept all OpenEBS local storage on the specified nodes.
        Creates a dm-dust device that will be used for all PVs created in /var/openebs/local/.

        This works by:
        1. Creating a large dm-dust device
        2. Mounting it at /var/openebs/local
        3. All PVs created by OpenEBS will automatically use this dm-dust device
        """
        if nodes is None:
            nodes_response = self.kubectl.list_nodes()
            nodes = [node.metadata.name for node in nodes_response.items]

        if not nodes:
            raise RuntimeError("No nodes available for dm-dust setup")

        for node in nodes:
            try:
                self._setup_dm_dust_on_node(node)
                print(f"[dm-dust] ✅ Set up dm-dust infrastructure on {node}")
            except Exception as e:
                print(f"[dm-dust] ❌ Failed to set up dm-dust on {node}: {e}")

    def _setup_dm_dust_on_node(self, node: str):
        """Set up dm-dust device to intercept OpenEBS storage on a single node."""
        openebs_path = "/var/openebs/local"
        pod = self._get_khaos_pod_on_node(node)

        inner_cmd = (
            "set -e; "
            "echo 'Setting up dm-dust for OpenEBS local storage...'; "
            "echo 'Checking dm_dust module...'; "
            "modprobe dm_dust || { echo 'Failed to load dm_dust module'; exit 1; }; "
            "lsmod | grep dm_dust || { echo 'dm_dust module not found in lsmod'; exit 1; }; "
            "echo 'Checking device-mapper targets...'; "
            "dmsetup targets | grep dust || { echo 'dust target not available in dmsetup'; exit 1; }; "
            f"echo 'Preparing OpenEBS directory at {openebs_path}...'; "
            f"rm -rf {shlex.quote(openebs_path)}/* 2>/dev/null || true; "
            f"mkdir -p {shlex.quote(openebs_path)}; "
            "echo 'Creating 1GB backing file for OpenEBS dm-dust (smaller for testing)...'; "
            "BACKING_FILE=/var/tmp/openebs_dm_dust.img; "
            "dd if=/dev/zero of=$BACKING_FILE bs=1M count=1024; "
            "echo 'Setting up loop device...'; "
            "LOOP_DEV=$(losetup -f --show $BACKING_FILE); "
            'echo "Loop device: $LOOP_DEV"; '
            "SECTORS=$(blockdev --getsz $LOOP_DEV); "
            'echo "Sectors: $SECTORS"; '
            "echo 'Creating healthy dm-dust device for OpenEBS...'; "
            "DM_NAME=openebs_dust; "
            "dmsetup remove $DM_NAME 2>/dev/null || true; "
            "echo 'Running dmsetup create command...'; "
            "dmsetup create $DM_NAME --table \"0 $SECTORS dust $LOOP_DEV 0 512\" --verbose || { echo 'dmsetup create failed'; dmsetup targets; exit 1; }; "
            "echo 'dmsetup create completed successfully'; "
            "echo 'Verifying dm device was created...'; "
            "ls -la /dev/mapper/$DM_NAME || { echo 'dm device not found'; exit 1; }; "
            "echo 'Formatting dm-dust device with ext4...'; "
            "mkfs.ext4 -F /dev/mapper/$DM_NAME || { echo 'mkfs.ext4 failed'; exit 1; }; "
            f"echo 'Mounting dm-dust device at {openebs_path}...'; "
            f"mount /dev/mapper/$DM_NAME {shlex.quote(openebs_path)}; "
            "echo 'Setting proper permissions...'; "
            f"chmod 755 {shlex.quote(openebs_path)}; "
            "echo 'OpenEBS dm-dust infrastructure ready - all PVs will use dm-dust'"
        )

        cmd = [
            "kubectl",
            "-n",
            self.khaos_ns,
            "exec",
            pod,
            "--",
            "nsenter",
            "--mount=/proc/1/ns/mnt",
            "bash",
            "-lc",
            inner_cmd,
        ]

        rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        if rc.returncode != 0:
            raise RuntimeError(
                f"Failed to setup dm-dust on {node}: rc={rc.returncode}, stdout={rc.stdout}, stderr={rc.stderr}"
            )

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
