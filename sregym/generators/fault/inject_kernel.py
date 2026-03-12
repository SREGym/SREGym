import json
import shlex
import subprocess
from collections.abc import Iterable

from sregym.service.kubectl import KubeCtl

# Constants
DEBUGFS_ROOT = "/sys/kernel/debug"
DEFAULT_KHAOS_NS = "khaos"
DEFAULT_KHAOS_LABEL = "app=khaos"
DEFAULT_LOOP_IMAGE = "/var/tmp/khaos-fault.img"
DEFAULT_DM_FLAKEY_NAME = "khaos_flakey0"
DEFAULT_SIZE_GB = 5

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

    def __init__(self, kubectl: KubeCtl, khaos_ns: str = DEFAULT_KHAOS_NS, khaos_label: str = DEFAULT_KHAOS_LABEL):
        self.kubectl = kubectl
        self.khaos_ns = khaos_ns
        self.khaos_label = khaos_label
        self._pod_cache: dict[str, str] = {}  # Cache pod names by node

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
        extra: dict[str, str] | None = None,
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
            raise RuntimeError("Failed to get pods: empty response")

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
                f"Capability path not found in pod {pod}: {path}. Is debugfs mounted and the kernel built with {cap}?"
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
        rc = subprocess.run(cmd, capture_output=True, text=True)
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

    def _exec_with_nsenter_mount(self, node: str, script: str, check: bool = True) -> tuple[int, str, str]:
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
        rc = subprocess.run(cmd, capture_output=True, text=True)
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
        self,
        node: str,
        name: str,
        up_interval: int,
        down_interval: int,
        features: str = "",
        offset_sectors: int = 0,
        num_features: int | None = None,
    ) -> None:
        """Reload a flakey device with new parameters."""
        name_q = shlex.quote(name)
        if features:
            count = len(features.split()) if num_features is None else num_features
            feat_tail = f" {count} {features}"
        else:
            feat_tail = ""

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
        dev: str | None = None,
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

    def drop_caches(self, node: str, show_log: bool = True) -> None:
        """
        Drop page cache, dentries, and inodes on the target node.
        This forces the application to read from the disk, hitting the bad blocks.
        """
        # echo 3 > /proc/sys/vm/drop_caches
        # We use sysctl -w vm.drop_caches=3 which is cleaner if available,
        # but writing to /proc is more universal.
        script = "echo 3 > /proc/sys/vm/drop_caches"
        self._exec_on_node(node, script)
        if show_log:
            print(f"[KernelInjector] Dropped caches on node {node}")
