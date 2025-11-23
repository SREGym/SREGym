import json
import shlex
import subprocess
from typing import Dict, Iterable, Optional, Tuple

from sregym.service.kubectl import KubeCtl

# Constants
DEBUGFS_ROOT = "/sys/kernel/debug"
DEFAULT_KHAOS_NS = "khaos"
DEFAULT_KHAOS_LABEL = "app=khaos"
DEFAULT_LOOP_IMAGE = "/var/tmp/khaos-fault.img"
DEFAULT_DM_FLAKEY_NAME = "khaos_flakey0"
DEFAULT_DM_DUST_NAME = "khaos_dust1"
DEFAULT_DM_LSE_NAME = "khaos_lse"
DEFAULT_BLOCK_SIZE = 512
DEFAULT_SIZE_GB = 5
PVC_BOUND_TIMEOUT = "60s"

# Supported fault capability directories under debugfs
FAULT_CAPS = {
    "failslab": f"{DEBUGFS_ROOT}/failslab",
    "fail_page_alloc": f"{DEBUGFS_ROOT}/fail_page_alloc",
    "fail_futex": f"{DEBUGFS_ROOT}/fail_futex",
    "fail_make_request": f"{DEBUGFS_ROOT}/fail_make_request",
    "fail_function": f"{DEBUGFS_ROOT}/fail_function",
    # add more if you enable them on your kernel (e.g., NVMe fault injectors)
}


class KernelInjector:
    """
    Control Linux kernel fault-injection infrastructure via debugfs from a Khaos DaemonSet pod.

    Typical use:
        kf = KernelInjector(kubectl, khaos_ns="khaos", khaos_label="app=khaos")
        kf.enable_fault(node="nodeX", cap="fail_page_alloc", probability=5, interval=1, times=-1)
        kf.set_task_filter_pids(node="nodeX", pids=[1234, 5678], enabled=True)   # scope to those PIDs
        ...
        kf.disable_fault(node="nodeX", cap="fail_page_alloc")
        kf.set_task_filter_pids(node="nodeX", pids=[1234, 5678], enabled=False)

    You can also inject function-specific errors:
        kf.fail_function_add(node, func="open_ctree", retval=-12)
        kf.fail_function_clear(node)

    And systematic "Nth call fails" per-task:
        kf.set_fail_nth(node, pid=1234, nth=10)  # the task's 10th faultable call fails
    """

    def __init__(
        self, kubectl: KubeCtl, khaos_ns: str = DEFAULT_KHAOS_NS, khaos_label: str = DEFAULT_KHAOS_LABEL
    ):
        self.kubectl = kubectl
        self.khaos_ns = khaos_ns
        self.khaos_label = khaos_label
        self._pod_cache: Dict[str, str] = {}  # Cache pod names by node
        self.recovery_data: Optional[Dict[str, str]] = None

    # ---------- Public API ----------

    def enable_fault(
        self,
        node: str,
        cap: str,
        *,
        probability: int = 100,
        interval: int = 1,
        times: int = -1,
        space: int = 0,
        verbose: int = 1,
        extra: Optional[Dict[str, str]] = None,
    ) -> None:
        """Enable a fault capability (e.g., fail_page_alloc) with the given knobs."""
        pod = self._get_khaos_pod_on_node(node)
        cap_path = self._cap_path_checked(pod, cap)
        self._ensure_debugfs(pod)

        # Core knobs
        knobs = {
            "probability": str(int(probability)),
            "interval": str(int(interval)),
            "times": str(int(times)),
            "space": str(int(space)),
            "verbose": str(int(verbose)),
        }
        if extra:
            knobs.update({k: str(v) for k, v in extra.items()})

        for key, value in knobs.items():
            self._write(pod, f"{cap_path}/{key}", value)

    def disable_fault(self, node: str, cap: str) -> None:
        """Disable a fault capability by setting probability=0."""
        pod = self._get_khaos_pod_on_node(node)
        cap_path = self._cap_path_checked(pod, cap)
        self._write(pod, f"{cap_path}/probability", "0")

    def set_task_filter(self, node: str, cap: str, enabled: bool) -> None:
        """Enable/disable task-filter for a capability (then mark PIDs with /proc/<pid>/make-it-fail=1)."""
        pod = self._get_khaos_pod_on_node(node)
        cap_path = self._cap_path_checked(pod, cap)
        self._write(pod, f"{cap_path}/task-filter", "Y" if enabled else "N")

    def set_task_filter_pids(self, node: str, pids: Iterable[int], enabled: bool) -> None:
        """
        Toggle /proc/<pid>/make-it-fail for each PID so task-filtered faults only hit those tasks.
        NOTE: This affects *all* capabilities with task-filter=Y.
        """
        pod = self._get_khaos_pod_on_node(node)
        val = "1" if enabled else "0"
        for pid in pids:
            self._write(pod, f"/proc/{int(pid)}/make-it-fail", val, must_exist=False)

    # --------- fail_function helpers ---------

    def fail_function_add(self, node: str, func: str, retval: int) -> None:
        """
        Add a function to the injection list and set its retval.
        The function must be annotated with ALLOW_ERROR_INJECTION() in the kernel.
        """
        pod = self._get_khaos_pod_on_node(node)
        base = self._cap_path_checked(pod, "fail_function")
        self._write(pod, f"{base}/inject", func)
        self._write(pod, f"{base}/{func}/retval", str(int(retval)))

        # Typical default knobs to ensure it triggers:
        self.enable_fault(node, "fail_function", probability=100, interval=1, times=-1, verbose=1)

    def fail_function_remove(self, node: str, func: str) -> None:
        """Remove a function from the injection list."""
        pod = self._get_khaos_pod_on_node(node)
        base = self._cap_path_checked(pod, "fail_function")
        # '!' prefix removes a function from injection list
        self._write(pod, f"{base}/inject", f"!{func}")

    def fail_function_clear(self, node: str) -> None:
        """Clear all functions from the injection list."""
        pod = self._get_khaos_pod_on_node(node)
        base = self._cap_path_checked(pod, "fail_function")
        # empty string clears the list
        self._write(pod, f"{base}/inject", "")

    # --------- per-task "Nth call fails" ---------

    def set_fail_nth(self, node: str, pid: int, nth: int) -> None:
        """
        Systematic faulting: write N to /proc/<pid>/fail-nth — that task's Nth faultable call will fail.
        Takes precedence over probability/interval.
        """
        pod = self._get_khaos_pod_on_node(node)
        self._write(pod, f"/proc/{int(pid)}/fail-nth", str(int(nth)), must_exist=True)

    # ---------- Internals ----------

    def _get_khaos_pod_on_node(self, node: str) -> str:
        """Get the Khaos pod name on the specified node, with caching."""
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

        raise RuntimeError(f"No running Khaos DS pod found on node {node}")

    def _cap_path_checked(self, pod: str, cap: str) -> str:
        """Validate and return the capability path."""
        if cap not in FAULT_CAPS:
            raise ValueError(f"Unsupported fault capability '{cap}'. Known: {', '.join(FAULT_CAPS)}")
        path = FAULT_CAPS[cap]
        if not self._exists(pod, path):
            raise RuntimeError(
                f"Capability path not found in pod {pod}: {path}. "
                f"Is debugfs mounted and the kernel built with {cap}?"
            )
        return path

    def _ensure_debugfs(self, pod: str) -> None:
        """Ensure debugfs is mounted."""
        if self._exists(pod, DEBUGFS_ROOT):
            return
        # Try to mount (usually not needed; your DS mounts host /sys/kernel/debug)
        self._sh(pod, f"mount -t debugfs none {shlex.quote(DEBUGFS_ROOT)} || true")

    # --- pod exec helpers ---

    def _exists(self, pod: str, path: str) -> bool:
        """Check if a path exists in the pod."""
        cmd = (
            f"kubectl -n {shlex.quote(self.khaos_ns)} exec {shlex.quote(pod)} -- "
            f"sh -lc 'test -e {shlex.quote(path)} && echo OK || true'"
        )
        out = self.kubectl.exec_command(cmd)
        return (out or "").strip() == "OK"

    def _write(self, pod: str, path: str, value: str, *, must_exist: bool = True) -> None:
        """Write a value to a path in the pod."""
        cmd = [
            "kubectl",
            "-n",
            self.khaos_ns,
            "exec",
            pod,
            "--",
            "sh",
            "-lc",
            f"printf %s {shlex.quote(value)} > {shlex.quote(path)} 2>/dev/null || true",
        ]
        rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if must_exist and rc.returncode != 0:
            raise RuntimeError(f"Failed to write '{value}' to {path} in {pod}: rc={rc.returncode}, err={rc.stderr}")

    def _sh(self, pod: str, script: str) -> str:
        """Execute a shell script in the pod."""
        cmd = ["kubectl", "-n", self.khaos_ns, "exec", pod, "--", "sh", "-lc", script]
        out = self.kubectl.exec_command(" ".join(shlex.quote(x) for x in cmd))
        return out or ""

    def _exec_on_node(self, node: str, script: str) -> str:
        """Execute a script on the node using nsenter (runs in the Khaos pod on that node)."""
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
            script,
        ]
        out = self.kubectl.exec_command(" ".join(shlex.quote(x) for x in cmd))
        return out or ""

    def _exec_with_nsenter_mount(self, node: str, script: str, check: bool = True) -> Tuple[int, str, str]:
        """Execute a script using nsenter with mount namespace, returns (returncode, stdout, stderr)."""
        pod = self._get_khaos_pod_on_node(node)
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
            script,
        ]
        rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and rc.returncode != 0:
            raise RuntimeError(
                f"Command failed on node {node}: rc={rc.returncode}, stdout={rc.stdout}, stderr={rc.stderr}"
            )
        return rc.returncode, rc.stdout, rc.stderr

    # ---------- loopback "test disk" helpers ----------

    def _loop_create(self, node: str, size_gb: int = DEFAULT_SIZE_GB) -> str:
        """Create a sparse file and attach a loop device. Returns /dev/loopN."""
        script = rf"""
set -e
mkdir -p /var/tmp
IMG={shlex.quote(DEFAULT_LOOP_IMAGE)}
[ -e "$IMG" ] || fallocate -l {int(size_gb)}G "$IMG"
LOOP=$(losetup -f --show "$IMG")
echo "$LOOP"
"""
        return self._exec_on_node(node, script).strip()

    def _loop_destroy(self, node: str) -> None:
        """Detach loop device created by _loop_create (best-effort)."""
        script = rf"""
IMG={shlex.quote(DEFAULT_LOOP_IMAGE)}
if losetup -j "$IMG" | awk -F: '{{print $1}}' | grep -q '/dev/loop'; then
    losetup -j "$IMG" | awk -F: '{{print $1}}' | xargs -r -n1 losetup -d || true
fi
"""
        self._exec_on_node(node, script)

    # ---------- dm-flakey ----------

    def dm_flakey_create(
        self, node: str, name: str, dev: str, up_s: int, down_s: int, offset_sectors: int = 0, features: str = ""
    ) -> None:
        """
        Create a flakey device:
          table: "0 <sectors> flakey <dev> <offset> <up> <down> [1 <features>]"
        features examples:
          - "drop_writes"
          - "error_writes"
          - "corrupt_bio_byte 32 r 1 0"
        """
        dev_q = shlex.quote(dev)
        name_q = shlex.quote(name)
        feat_tail = f" 1 {features}" if features else ""
        script = rf"""
set -e
modprobe dm_flakey || true
SECTORS=$(blockdev --getsz {dev_q})
dmsetup create {name_q} --table "0 $SECTORS flakey {dev_q} {int(offset_sectors)} {int(up_s)} {int(down_s)}{feat_tail}"
"""
        self._exec_on_node(node, script)

    def dm_target_remove(self, node: str, name: str) -> None:
        """Remove a device mapper target."""
        self._exec_on_node(node, f"dmsetup remove {shlex.quote(name)} 2>/dev/null || true")

    def dm_flakey_reload(
        self, node: str, name: str, up_interval: int, down_interval: int, features: str = "", offset_sectors: int = 0
    ) -> None:
        """Reload a flakey device with new parameters."""
        name_q = shlex.quote(name)
        feat_tail = f" {len(features.split())} {features}" if features else ""

        script = rf"""
set -e
# Get the underlying device from current table
UNDERLYING=$(dmsetup table {name_q} | awk '{{print $4}}')
SECTORS=$(dmsetup table {name_q} | awk '{{print $2}}')

echo "Reloading {name_q}: up={up_interval}s down={down_interval}s features='{features}'"
echo "Underlying device: $UNDERLYING, Sectors: $SECTORS"

# Reload the table with new parameters
dmsetup reload {name_q} --table "0 $SECTORS flakey $UNDERLYING {int(offset_sectors)} {int(up_interval)} {int(down_interval)}{feat_tail}"

# Activate the new table (this is atomic, no unmount needed)
dmsetup resume {name_q}

echo "dm-flakey device reloaded successfully"
dmsetup status {name_q}
"""
        result = self._exec_on_node(node, script)
        print(f"[dm-flakey] Reload result: {result.strip()}")

    # ---------- dm-dust ----------

    def dm_dust_create(self, node: str, name: str, dev: str, blksz: int = DEFAULT_BLOCK_SIZE, offset: int = 0) -> None:
        """
        Create a dust device that can simulate bad sectors.
          table: "0 <sectors> dust <dev> <offset> <blksz>"
        """
        dev_q = shlex.quote(dev)
        name_q = shlex.quote(name)
        script = rf"""
set -e
modprobe dm_dust || true
SECTORS=$(blockdev --getsz {dev_q})
dmsetup create {name_q} --table "0 $SECTORS dust {dev_q} {int(offset)} {int(blksz)}"
"""
        self._exec_on_node(node, script)

    def dm_dust_add_badblocks(self, node: str, name: str, blocks: list[int]) -> None:
        """Add bad blocks to a dust device."""
        name_q = shlex.quote(name)
        blocks_str = " ".join(str(int(b)) for b in blocks)

        script = f"""
DM_NAME={name_q}
BLOCKS="{blocks_str}"
SUCCESS=0
FAILED=0
for BLOCK in $BLOCKS; do
    if dmsetup message $DM_NAME 0 addbadblock $BLOCK 2>/dev/null; then
        SUCCESS=$((SUCCESS + 1))
    else
        FAILED=$((FAILED + 1))
    fi
done
echo "Added $SUCCESS bad blocks, $FAILED already existed or failed"
"""
        result = self._exec_on_node(node, script)
        print(f"[dm-dust] {result.strip()}")

    def dm_dust_add_badblocks_range(self, node: str, name: str, start: int, end: int, step: int) -> None:
        """Add bad blocks using parallel execution with xargs for speed."""
        name_q = shlex.quote(name)

        script = f"""
echo "Adding bad blocks from {start} to {end} with step {step} (parallel)..."
START_TIME=$(date +%s)

seq {start} {step} {end} | xargs -P 32 -I {{}} sh -c 'dmsetup message {name_q} 0 addbadblock {{}} 2>/dev/null' || true

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
COUNT=$(seq {start} {step} {end} | wc -l)
echo "Completed: Added approximately $COUNT bad blocks in $DURATION seconds"
"""
        result = self._exec_on_node(node, script)
        print(f"[dm-dust] {result.strip()}")

    def dm_dust_enable(self, node: str, name: str) -> None:
        """Enable bad block simulation on a dust device."""
        name_q = shlex.quote(name)
        result = self._exec_on_node(node, f"dmsetup message {name_q} 0 enable && dmsetup status {name_q}")
        print(f"[dm-dust] Enabled. Status: {result.strip()}")

    def dm_dust_disable(self, node: str, name: str) -> None:
        """Disable bad block simulation on a dust device."""
        name_q = shlex.quote(name)
        result = self._exec_on_node(node, f"dmsetup message {name_q} 0 disable && dmsetup status {name_q}")
        print(f"[dm-dust] Disabled. Status: {result.strip()}")

    def dm_dust_clear(self, node: str, name: str) -> None:
        """Clear all bad blocks from the device."""
        name_q = shlex.quote(name)
        result = self._exec_on_node(node, f"dmsetup message {name_q} 0 clearbadblocks 2>&1 || true")
        print(f"[dm-dust] Clear bad blocks: {result.strip()}")

    def dm_dust_list(self, node: str, name: str) -> str:
        """List all bad blocks on a dust device."""
        return self._exec_on_node(node, f"dmsetup message {shlex.quote(name)} 0 listbadblocks").strip()

    # ---------- "one-liner" recipes ----------

    def add_bad_blocks(self, node: str, dm_device_name: str, blocks: list[int]) -> None:
        """Convenience wrapper for adding bad blocks."""
        self.dm_dust_add_badblocks(node, dm_device_name, blocks)

    def enable_bad_blocks(self, node: str, dm_device_name: str, enable: bool = True) -> None:
        """Convenience wrapper for enabling/disabling bad blocks."""
        if enable:
            self.dm_dust_enable(node, dm_device_name)
        else:
            self.dm_dust_disable(node, dm_device_name)

    # ---------- Disk fault injection helpers ----------

    def _format_and_mount(self, node: str, mapper: str, mount_point: str) -> None:
        """Format and mount a device mapper device."""
        script = rf"""
set -e
if ! blkid {shlex.quote(mapper)} >/dev/null 2>&1; then
    mkfs.ext4 -F {shlex.quote(mapper)} >/dev/null 2>&1 || true
fi
mkdir -p {shlex.quote(mount_point)}
mount {shlex.quote(mapper)} {shlex.quote(mount_point)} 2>/dev/null || true
echo {shlex.quote(mapper)}
"""
        self._exec_on_node(node, script)

    def _unmount_and_cleanup(self, node: str, mount_point: str, dm_name: str) -> None:
        """Unmount and remove a device mapper target."""
        script = rf"""
umount {shlex.quote(mount_point)} 2>/dev/null || true
dmsetup remove {shlex.quote(dm_name)} 2>/dev/null || true
"""
        self._exec_on_node(node, script)

    def inject_disk_outage(
        self,
        node: str,
        up_s: int = 10,
        down_s: int = 5,
        features: str = "",
        dev: Optional[str] = None,
        name: str = DEFAULT_DM_FLAKEY_NAME,
        size_gb: int = DEFAULT_SIZE_GB,
    ) -> str:
        """
        Create a flakey DM device on the specified node.
        If dev is None, creates a safe loopback disk of size_gb and wraps it.
        Returns the mapper path (/dev/mapper/<name>) you can mount/use for tests.
        """
        if dev is None:
            dev = self._loop_create(node, size_gb=size_gb)

        self.dm_flakey_create(node, name=name, dev=dev, up_s=up_s, down_s=down_s, features=features)
        mapper = f"/dev/mapper/{name}"
        mount_point = f"/mnt/{name}"
        self._format_and_mount(node, mapper, mount_point)
        return mapper

    def recover_disk_outage(self, node: str, name: str = DEFAULT_DM_FLAKEY_NAME) -> None:
        """Unmount and remove the flakey target; also detach loop if we created one."""
        mount_point = f"/mnt/{name}"
        self._unmount_and_cleanup(node, mount_point, name)
        # Best effort detach loop used by our default image path
        self._loop_destroy(node)

    def inject_badblocks(
        self,
        node: str,
        blocks: Optional[list[int]] = None,
        dev: Optional[str] = None,
        name: str = DEFAULT_DM_DUST_NAME,
        blksz: int = DEFAULT_BLOCK_SIZE,
        size_gb: int = DEFAULT_SIZE_GB,
        enable: bool = True,
    ) -> str:
        """
        Create a dust DM device and (optionally) enable failing reads on listed blocks.
        If dev is None, creates a loopback disk of size_gb and wraps it.
        Returns /dev/mapper/<name>.
        """
        if dev is None:
            dev = self._loop_create(node, size_gb=size_gb)

        self.dm_dust_create(node, name=name, dev=dev, blksz=blksz)
        if blocks:
            self.dm_dust_add_badblocks(node, name, blocks)
        if enable:
            self.dm_dust_enable(node, name)

        mapper = f"/dev/mapper/{name}"
        mount_point = f"/mnt/{name}"
        self._format_and_mount(node, mapper, mount_point)
        return mapper

    def recover_badblocks(self, node: str, name: str = DEFAULT_DM_DUST_NAME) -> None:
        """Unmount and remove the dust target and detach loop if present."""
        mount_point = f"/mnt/{name}"
        self._unmount_and_cleanup(node, mount_point, name)
        self._loop_destroy(node)

    # ---------- LSE (Latent Sector Error) injection ----------

    def _get_pvc_info(self, pvc_name: str, namespace: str) -> Dict[str, str]:
        """Get PVC information including PV details."""
        out = self.kubectl.exec_command(f"kubectl -n {namespace} get pvc {pvc_name} -o json")
        if not out:
            raise RuntimeError(f"Failed to get PVC {pvc_name} in namespace {namespace}")
        pvc = json.loads(out)
        pv_name = pvc["spec"]["volumeName"]

        out = self.kubectl.exec_command(f"kubectl get pv {pv_name} -o json")
        if not out:
            raise RuntimeError(f"Failed to get PV {pv_name}")
        pv = json.loads(out)

        return {
            "pv_name": pv_name,
            "capacity": pv["spec"]["capacity"]["storage"],
            "storage_class": pv["spec"]["storageClassName"],
            "local_path": pv["spec"]["local"]["path"],
        }

    def _create_dm_dust_for_lse(self, node: str, local_path: str) -> None:
        """Create a dm-dust device wrapping the device at local_path for LSE (Latent Sector Error) simulation."""
        script = f"""
set -e
echo 'Checking for dm_dust module...'
if ! lsmod | grep -q dm_dust; then
    echo 'Loading dm_dust module...'
    modprobe dm_dust || (echo 'ERROR: dm_dust module not available. Try running: sudo modprobe dm_dust' && exit 1)
else
    echo 'dm_dust module already loaded'
fi

echo 'Finding device for {shlex.quote(local_path)}...'
dev=$(findmnt -no SOURCE {shlex.quote(local_path)})
if [ -z "$dev" ]; then
    echo 'ERROR: No device found for mount point {shlex.quote(local_path)}'
    exit 1
fi

echo "Found device: $dev"
if [ ! -b "$dev" ]; then
    echo "ERROR: Device $dev is not a block device"
    exit 1
fi

echo 'Getting device size...'
SECTORS=$(blockdev --getsz $dev)
if [ "$SECTORS" -eq 0 ]; then
    echo 'ERROR: Device has 0 sectors'
    exit 1
fi

echo "Device size: $SECTORS sectors"
echo 'Removing existing {DEFAULT_DM_LSE_NAME} if present...'
dmsetup remove {DEFAULT_DM_LSE_NAME} 2>/dev/null || true

echo 'Creating dm-dust device...'
dmsetup create {DEFAULT_DM_LSE_NAME} --table "0 $SECTORS dust $dev 0 {DEFAULT_BLOCK_SIZE}" || (
    echo 'ERROR: Failed to create dm-dust device'
    dmsetup targets
    exit 1
)

echo 'dm-dust device created successfully'
dmsetup info {DEFAULT_DM_LSE_NAME}
"""
        rc, stdout, stderr = self._exec_with_nsenter_mount(node, script, check=True)
        print(f"[DEBUG] Command output: {stdout}")
        if stderr:
            print(f"[DEBUG] Command stderr: {stderr}")

    def _generate_pv_yaml(
        self, pv_name: str, capacity: str, storage_class: str, local_path: str, node: str
    ) -> str:
        """Generate PersistentVolume YAML."""
        return f"""apiVersion: v1
kind: PersistentVolume
metadata:
  name: {pv_name}
spec:
  capacity:
    storage: {capacity}
  accessModes:
  - ReadWriteOnce
  storageClassName: {storage_class}
  local:
    path: {local_path}
  nodeAffinity:
    required:
      nodeSelectorTerms:
      - matchExpressions:
        - key: kubernetes.io/hostname
          operator: In
          values:
          - {node}
  persistentVolumeReclaimPolicy: Delete
"""

    def _generate_pvc_yaml(self, pvc_name: str, namespace: str, capacity: str, storage_class: str, pv_name: str) -> str:
        """Generate PersistentVolumeClaim YAML."""
        return f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {pvc_name}
  namespace: {namespace}
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: {capacity}
  storageClassName: {storage_class}
  volumeName: {pv_name}
"""

    def _apply_yaml(self, yaml_content: str) -> None:
        """Apply YAML content via kubectl."""
        self.kubectl.exec_command("kubectl apply -f -", input_data=yaml_content)

    def _wait_for_pvc_bound(self, pvc_name: str, namespace: str) -> None:
        """Wait for PVC to become Bound."""
        cmd = [
            "kubectl",
            "-n",
            namespace,
            "wait",
            f"pvc/{pvc_name}",
            "--for=condition=Bound",
            f"--timeout={PVC_BOUND_TIMEOUT}",
        ]
        rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if rc.returncode != 0:
            raise RuntimeError(f"PVC {pvc_name} did not become Bound: rc={rc.returncode}, err={rc.stderr}")

    def inject_lse(self, node: str, pvc_name: str, namespace: str) -> None:
        """
        Replace the target PVC with a faulty one backed by dm-dust.
        This simulates Latent Sector Errors (LSE) by wrapping the underlying device.
        """
        # 1. Get PVC and PV information
        pv_info = self._get_pvc_info(pvc_name, namespace)
        pv_name = pv_info["pv_name"]
        local_path = pv_info["local_path"]

        # Store recovery data
        self.recovery_data = {
            "node": node,
            "pvc_name": pvc_name,
            "namespace": namespace,
            **pv_info,
        }

        # 2. Wrap underlying device with dm-dust
        self._create_dm_dust_for_lse(node, local_path)

        # 3. Delete PV, then PVC
        self.kubectl.exec_command(f"kubectl delete pv {pv_name}")
        self.kubectl.exec_command(f"kubectl -n {namespace} delete pvc {pvc_name}")

        # 4. Recreate PV pointing at /dev/mapper/khaos_lse
        faulty_pv_path = f"/dev/mapper/{DEFAULT_DM_LSE_NAME}"
        new_pv_yaml = self._generate_pv_yaml(
            pv_name, pv_info["capacity"], pv_info["storage_class"], faulty_pv_path, node
        )
        self._apply_yaml(new_pv_yaml)

        # 5. Recreate PVC
        new_pvc_yaml = self._generate_pvc_yaml(
            pvc_name, namespace, pv_info["capacity"], pv_info["storage_class"], pv_name
        )
        self._apply_yaml(new_pvc_yaml)

        # 6. Wait until PVC is Bound
        self._wait_for_pvc_bound(pvc_name, namespace)

        print(f"[KernelInjector] Faulty PVC {pvc_name} reattached via dm-dust and Bound")

    def recover_lse(self) -> None:
        """Restore the original PVC/PV pointing at the raw device."""
        if not self.recovery_data:
            print("[KernelInjector] No recovery data found, cannot recover LSE")
            return

        data = self.recovery_data
        node = data["node"]
        pvc_name = data["pvc_name"]
        namespace = data["namespace"]
        pv_name = data["pv_name"]

        # Clean up dm-dust device first
        script = f"dmsetup remove {DEFAULT_DM_LSE_NAME} 2>/dev/null || true"
        self._exec_with_nsenter_mount(node, script, check=False)

        # Delete faulty PVC + PV
        self.kubectl.exec_command(f"kubectl -n {namespace} delete pvc {pvc_name}")
        self.kubectl.exec_command(f"kubectl delete pv {pv_name}")

        # Recreate clean PV
        healthy_pv_yaml = self._generate_pv_yaml(
            pv_name, data["capacity"], data["storage_class"], data["local_path"], node
        )
        self._apply_yaml(healthy_pv_yaml)

        # Recreate PVC
        healthy_pvc_yaml = self._generate_pvc_yaml(
            pvc_name, namespace, data["capacity"], data["storage_class"], pv_name
        )
        self._apply_yaml(healthy_pvc_yaml)

        print(f"[KernelInjector] PVC {pvc_name} restored to healthy device")
        self.recovery_data = None

    def drop_caches(self, node: str) -> None:
        """
        Drop page cache, dentries, and inodes on the target node.
        This forces the application to read from the disk, hitting the bad blocks.
        """
        # echo 3 > /proc/sys/vm/drop_caches
        # We use sysctl -w vm.drop_caches=3 which is cleaner if available,
        # but writing to /proc is more universal.
        script = "echo 3 > /proc/sys/vm/drop_caches"
        self._exec_on_node(node, script)
        print(f"[KernelInjector] Dropped caches on node {node}")
