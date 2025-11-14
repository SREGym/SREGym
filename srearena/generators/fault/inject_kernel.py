import json
import shlex
import subprocess
from typing import Dict, Iterable, Optional

from srearena.service.kubectl import KubeCtl

DEBUGFS_ROOT = "/sys/kernel/debug"

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
        kf = KernelFaults(kubectl, khaos_ns="khaos", khaos_label="app=khaos")
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

    def __init__(self, kubectl: KubeCtl, khaos_ns: str = "khaos", khaos_label: str = "app=khaos"):
        self.kubectl = kubectl
        self.khaos_ns = khaos_ns
        self.khaos_label = khaos_label

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
        self._write(pod, f"{cap_path}/probability", str(int(probability)))
        self._write(pod, f"{cap_path}/interval", str(int(interval)))
        self._write(pod, f"{cap_path}/times", str(int(times)))
        self._write(pod, f"{cap_path}/space", str(int(space)))
        self._write(pod, f"{cap_path}/verbose", str(int(verbose)))

        # Optional capability-specific knobs
        if extra:
            for k, v in extra.items():
                self._write(pod, f"{cap_path}/{k}", str(v))

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
        self._write(pod, f"{base}/probability", "100")
        self._write(pod, f"{base}/interval", "1")
        self._write(pod, f"{base}/times", "-1")
        self._write(pod, f"{base}/verbose", "1")

    def fail_function_remove(self, node: str, func: str) -> None:
        pod = self._get_khaos_pod_on_node(node)
        base = self._cap_path_checked(pod, "fail_function")
        # '!' prefix removes a function from injection list
        self._write(pod, f"{base}/inject", f"!{func}")

    def fail_function_clear(self, node: str) -> None:
        pod = self._get_khaos_pod_on_node(node)
        base = self._cap_path_checked(pod, "fail_function")
        # empty string clears the list
        self._write(pod, f"{base}/inject", "")

    # --------- per-task "Nth call fails" ---------

    def set_fail_nth(self, node: str, pid: int, nth: int) -> None:
        """
        Systematic faulting: write N to /proc/<pid>/fail-nth — that task’s Nth faultable call will fail.
        Takes precedence over probability/interval.
        """
        pod = self._get_khaos_pod_on_node(node)
        self._write(pod, f"/proc/{int(pid)}/fail-nth", str(int(nth)), must_exist=True)

    # ---------- Internals ----------

    def _get_khaos_pod_on_node(self, node: str) -> str:
        # Same discovery pattern you already use elsewhere:
        cmd = f"kubectl -n {shlex.quote(self.khaos_ns)} get pods -l {shlex.quote(self.khaos_label)} -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        import json as _json

        data = _json.loads(out or "{}")
        for item in data.get("items", []):
            if item.get("spec", {}).get("nodeName") == node and item.get("status", {}).get("phase") == "Running":
                return item["metadata"]["name"]
        raise RuntimeError(f"No running Khaos DS pod found on node {node}")

    def _cap_path_checked(self, pod: str, cap: str) -> str:
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
        if self._exists(pod, DEBUGFS_ROOT):
            return
        # Try to mount (usually not needed; your DS mounts host /sys/kernel/debug)
        self._sh(pod, f"mount -t debugfs none {shlex.quote(DEBUGFS_ROOT)} || true")

    # --- pod exec helpers ---

    def _exists(self, pod: str, path: str) -> bool:
        cmd = f"kubectl -n {shlex.quote(self.khaos_ns)} exec {shlex.quote(pod)} -- sh -lc 'test -e {shlex.quote(path)} && echo OK || true'"
        out = self.kubectl.exec_command(cmd)
        out = out[0] if isinstance(out, tuple) else out
        return (out or "").strip() == "OK"

    def _write(self, pod: str, path: str, value: str, *, must_exist: bool = True) -> None:
        # Safe echo via sh -lc with proper quoting
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
        cmd = ["kubectl", "-n", self.khaos_ns, "exec", pod, "--", "sh", "-lc", script]
        out = self.kubectl.exec_command(" ".join(shlex.quote(x) for x in cmd))
        return out[0] if isinstance(out, tuple) else (out or "")

    # ---------- generic exec on node (runs in the Khaos pod on that node) ----------
    def _exec_on_node(self, node: str, script: str) -> str:
        pod = self._get_khaos_pod_on_node(node)
        cmd = ["kubectl", "-n", self.khaos_ns, "exec", pod, "--", "nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "sh", "-c", script]
        out = self.kubectl.exec_command(" ".join(shlex.quote(x) for x in cmd))
        return out[0] if isinstance(out, tuple) else (out or "")

    # ---------- loopback “test disk” helpers (safe default) ----------
    def _loop_create(self, node: str, size_gb: int = 5) -> str:
        """Create a sparse file and attach a loop device. Returns /dev/loopN."""
        script = rf"""
            set -e
            mkdir -p /var/tmp
            IMG=/var/tmp/khaos-fault.img
            [ -e "$IMG" ] || fallocate -l {int(size_gb)}G "$IMG"
            LOOP=$(losetup -f --show "$IMG")
            echo "$LOOP"
        """
        return self._exec_on_node(node, script).strip()

    def _loop_destroy(self, node: str):
        """Detach loop device created by _loop_create (best-effort)."""
        script = r"""
            IMG=/var/tmp/khaos-fault.img
            if losetup -j "$IMG" | awk -F: '{print $1}' | grep -q '/dev/loop'; then
            losetup -j "$IMG" | awk -F: '{print $1}' | xargs -r -n1 losetup -d || true
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
        self._exec_on_node(node, f"dmsetup remove {shlex.quote(name)} 2>/dev/null || true")

    def dm_flakey_reload(
        self, node: str, name: str, up_interval: int, down_interval: int, features: str = "", offset_sectors: int = 0
    ) -> None:

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
    def dm_dust_create(self, node: str, name: str, dev: str, blksz: int = 512, offset: int = 0) -> None:
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
        name_q = shlex.quote(name)
        blocks_str = ' '.join(str(int(b)) for b in blocks)
        
        # Single shell command that loops through all blocks
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
        
        # Use seq to generate block numbers, pipe to xargs -P for parallel execution
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
        name_q = shlex.quote(name)
        result = self._exec_on_node(node, f"dmsetup message {name_q} 0 enable && dmsetup status {name_q}")
        print(f"[dm-dust] Enabled. Status: {result.strip()}")

    def dm_dust_disable(self, node: str, name: str) -> None:
        name_q = shlex.quote(name)
        result = self._exec_on_node(node, f"dmsetup message {name_q} 0 disable && dmsetup status {name_q}")
        print(f"[dm-dust] Disabled. Status: {result.strip()}")

    def dm_dust_clear(self, node: str, name: str) -> None:
        """Clear all bad blocks from the device."""
        name_q = shlex.quote(name)
        result = self._exec_on_node(node, f"dmsetup message {name_q} 0 clearbadblocks 2>&1 || true")
        print(f"[dm-dust] Clear bad blocks: {result.strip()}")

    def dm_dust_list(self, node: str, name: str) -> str:
        return self._exec_on_node(node, f"dmsetup message {shlex.quote(name)} 0 listbadblocks").strip()

    # ---------- "one-liner" recipes ----------
    def add_bad_blocks(self, node: str, dm_device_name: str, blocks: list[int]) -> None:
        self.dm_dust_add_badblocks(node, dm_device_name, blocks)

    def enable_bad_blocks(self, node: str, dm_device_name: str, enable: bool = True) -> None:
        self.dm_dust_enable(node, dm_device_name, enable=enable)

    
    def inject_disk_outage(
        self,
        node: str,
        up_s: int = 10,
        down_s: int = 5,
        features: str = "",
        dev: str | None = None,
        name: str = "khaos_flakey0",
        size_gb: int = 5,
    ) -> str:
        """
        Create a flakey DM device on the specified node.
        If dev is None, creates a safe loopback disk of size_gb and wraps it.
        Returns the mapper path (/dev/mapper/<name>) you can mount/use for tests.
        """
        loop = None
        if dev is None:
            loop = self._loop_create(node, size_gb=size_gb)
            dev = loop

        self.dm_flakey_create(node, name=name, dev=dev, up_s=up_s, down_s=down_s, features=features)
        mapper = f"/dev/mapper/{name}"
        # optional quick-format & mount point for convenience
        self._exec_on_node(
            node,
            rf"""
  set -e
  if ! blkid {shlex.quote(mapper)} >/dev/null 2>&1; then
    mkfs.ext4 -F {shlex.quote(mapper)} >/dev/null 2>&1 || true
  fi
  mkdir -p /mnt/{name}
  mount {shlex.quote(mapper)} /mnt/{name} 2>/dev/null || true
  echo {shlex.quote(mapper)}
""",
        )
        return mapper

    def recover_disk_outage(self, node: str, name: str = "khaos_flakey0") -> None:
        """Unmount and remove the flakey target; also detach loop if we created one."""
        mapper = f"/dev/mapper/{name}"
        self._exec_on_node(
            node,
            rf"""
umount /mnt/{name} 2>/dev/null || true
dmsetup remove {shlex.quote(name)} 2>/dev/null || true
""",
        )
        # Best effort detach loop used by our default image path
        self._loop_destroy(node)

    def inject_badblocks(
        self,
        node: str,
        blocks: list[int] | None = None,
        dev: str | None = None,
        name: str = "khaos_dust1",
        blksz: int = 512,
        size_gb: int = 5,
        enable: bool = True,
    ) -> str:
        """
        Create a dust DM device and (optionally) enable failing reads on listed blocks.
        If dev is None, creates a loopback disk of size_gb and wraps it.
        Returns /dev/mapper/<name>.
        """
        loop = None
        if dev is None:
            loop = self._loop_create(node, size_gb=size_gb)
            dev = loop

        self.dm_dust_create(node, name=name, dev=dev, blksz=blksz)
        if blocks:
            self.dm_dust_add_badblocks(node, name, blocks)
        if enable:
            self.dm_dust_enable(node, name, True)

        mapper = f"/dev/mapper/{name}"
        # Optionally mount for convenience (not required)
        self._exec_on_node(
            node,
            rf"""
  set -e
  if ! blkid {shlex.quote(mapper)} >/dev/null 2>&1; then
    mkfs.ext4 -F {shlex.quote(mapper)} >/dev/null 2>&1 || true
  fi
  mkdir -p /mnt/{name}
  mount {shlex.quote(mapper)} /mnt/{name} 2>/dev/null || true
  echo {shlex.quote(mapper)}
""",
        )
        return mapper

    def recover_badblocks(self, node: str, name: str = "khaos_dust1") -> None:
        """Unmount and remove the dust target and detach loop if present."""
        mapper = f"/dev/mapper/{name}"
        self._exec_on_node(
            node,
            rf"""
umount /mnt/{name} 2>/dev/null || true
dmsetup remove {shlex.quote(name)} 2>/dev/null || true
""",
        )
        self._loop_destroy(node)

    def inject_lse(self, node: str, pvc_name: str, namespace: str):
        """
        Replace the target PVC with a faulty one backed by dm-dust.
        """
        pod = self._get_khaos_pod_on_node(node)

        # 1. Get the bound PV
        out = self.kubectl.exec_command(f"kubectl -n {namespace} get pvc {pvc_name} -o json")
        if isinstance(out, tuple):
            out = out[0]
        pvc = json.loads(out)
        pv_name = pvc["spec"]["volumeName"]

        # Get capacity and storageClass
        out = self.kubectl.exec_command(f"kubectl get pv {pv_name} -o json")
        if isinstance(out, tuple):
            out = out[0]
        pv = json.loads(out)
        capacity = pv["spec"]["capacity"]["storage"]
        storage_class = pv["spec"]["storageClassName"]
        local_path = pv["spec"]["local"]["path"]

        # Store these for recovery
        self.recovery_data = {
            "node": node,
            "pvc_name": pvc_name,
            "namespace": namespace,
            "local_path": local_path,
            "pv_name": pv_name,
            "capacity": capacity,
            "storage_class": storage_class,
        }

        # 2. Wrap underlying device with dm-dust
        inner_cmd = (
            "set -e; "
            "echo 'Checking for dm_dust module...'; "
            "if ! lsmod | grep -q dm_dust; then "
            "  echo 'Loading dm_dust module...'; "
            "  modprobe dm_dust || (echo 'ERROR: dm_dust module not available. Try running: sudo modprobe dm_dust' && exit 1); "
            "else "
            "  echo 'dm_dust module already loaded'; "
            "fi; "
            f"echo 'Finding device for {local_path}...'; "
            f"dev=$(findmnt -no SOURCE {shlex.quote(local_path)}); "
            'if [ -z "$dev" ]; then '
            f"  echo 'ERROR: No device found for mount point {local_path}'; "
            "  exit 1; "
            "fi; "
            'echo "Found device: $dev"; '
            'if [ ! -b "$dev" ]; then '
            '  echo "ERROR: Device $dev is not a block device"; '
            "  exit 1; "
            "fi; "
            "echo 'Getting device size...'; "
            "SECTORS=$(blockdev --getsz $dev); "
            'if [ "$SECTORS" -eq 0 ]; then '
            "  echo 'ERROR: Device has 0 sectors'; "
            "  exit 1; "
            "fi; "
            'echo "Device size: $SECTORS sectors"; '
            "echo 'Removing existing khaos_lse if present...'; "
            "dmsetup remove khaos_lse 2>/dev/null || true; "
            "echo 'Creating dm-dust device...'; "
            "dmsetup create khaos_lse --table \"0 $SECTORS dust $dev 0 512\" || (echo 'ERROR: Failed to create dm-dust device' && dmsetup targets && exit 1); "
            "echo 'dm-dust device created successfully'; "
            "dmsetup info khaos_lse"
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
        rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        print(f"[DEBUG] Command output: {rc.stdout}")
        if rc.stderr:
            print(f"[DEBUG] Command stderr: {rc.stderr}")
        if rc.returncode != 0:
            raise RuntimeError(
                f"Failed to create dm-dust device on node {node}: rc={rc.returncode}, stdout={rc.stdout}, stderr={rc.stderr}"
            )

        # 3. Delete PV, then PVC
        self.kubectl.exec_command(f"kubectl delete pv {pv_name}")
        self.kubectl.exec_command(f"kubectl -n {namespace} delete pvc {pvc_name}")

        # 4. Recreate PV (pointing at /dev/mapper/khaos_lse)
        new_pv = f"""apiVersion: v1
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
        path: /dev/mapper/khaos_lse
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
        self.kubectl.exec_command("kubectl apply -f -", input_data=new_pv)

        # 5. Recreate PVC
        new_pvc = f"""apiVersion: v1
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
        self.kubectl.exec_command("kubectl apply -f -", input_data=new_pvc)

        # 6. Wait until PVC is Bound
        rc = subprocess.run(
            ["kubectl", "-n", namespace, "wait", f"pvc/{pvc_name}", "--for=condition=Bound", "--timeout=60s"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if rc.returncode != 0:
            raise RuntimeError(f"PVC {pvc_name} did not become Bound: rc={rc.returncode}, err={rc.stderr}")

        print(f"[KernelInjector] Faulty PVC {pvc_name} reattached via dm-dust and Bound")

    def recover_lse(self):
        """
        Restore the original PVC/PV pointing at the raw device.
        """
        if not hasattr(self, "recovery_data"):
            print("[KernelInjector] No recovery data found, cannot recover LSE")
            return

        data = self.recovery_data
        node = data["node"]
        pvc_name = data["pvc_name"]
        namespace = data["namespace"]
        local_path = data["local_path"]
        pv_name = data["pv_name"]
        capacity = data["capacity"]
        storage_class = data["storage_class"]

        # Clean up dm-dust device first
        pod = self._get_khaos_pod_on_node(node)
        inner_cmd = "dmsetup remove khaos_lse 2>/dev/null || true"
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
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Delete faulty PVC + PV
        self.kubectl.exec_command(f"kubectl -n {namespace} delete pvc {pvc_name}")
        self.kubectl.exec_command(f"kubectl delete pv {pv_name}")

        # Recreate clean PV
        healthy_pv = f"""apiVersion: v1
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
  persistentVolumeReclaimPolicy: Delete"""
        self.kubectl.exec_command("kubectl apply -f -", input_data=healthy_pv)

        # Recreate PVC
        healthy_pvc = f"""apiVersion: v1
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
  volumeName: {pv_name}"""
        self.kubectl.exec_command("kubectl apply -f -", input_data=healthy_pvc)

        print(f"[KernelInjector] PVC {pvc_name} restored to healthy device")
