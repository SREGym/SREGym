import hashlib
import json
import shlex
import subprocess

from sregym.service.kubectl import KubeCtl

DEFAULT_KHAOS_NS = "khaos"
DEFAULT_KHAOS_LABEL = "app=khaos"
DM_FLAKEY_DEVICE_NAME = "openebs_flakey"
DM_FLAKEY_BACKING_FILE_SIZE_GB = 5
OPENEBS_LOCAL_PATH = "/var/openebs/khaos"
DM_FLAKEY_STORAGE_CLASS = "sregym-dm-flakey"
SETUP_TIMEOUT_SECONDS = 120
RANDOM_CORRUPTION_FEATURES = "random_read_corrupt 1000000000 random_write_corrupt 1000000000"


def dm_flakey_preflight_script(*, random_corruption: bool = False) -> str:
    """Exercise a disposable device; module presence alone is insufficient."""
    features = f"4 {RANDOM_CORRUPTION_FEATURES}" if random_corruption else "0"
    return f"""set -eu
command -v dmsetup >/dev/null
command -v losetup >/dev/null
command -v mkfs.ext4 >/dev/null
modprobe dm_flakey
dmsetup targets | grep -qw flakey
workdir=$(mktemp -d /var/tmp/khaos-dm-check.XXXXXXXX)
device=khaos_check_$(basename "$workdir")
loop=
cleanup() {{
    umount "$workdir/mount" 2>/dev/null || true
    dmsetup remove --noudevsync "$device" 2>/dev/null || true
    if [ -n "$loop" ]; then losetup -d "$loop"; fi
    rm -rf "$workdir"
}}
trap cleanup EXIT
trap 'exit 124' TERM INT
truncate -s 32M "$workdir/backing.img"
loop=$(losetup --find --show "$workdir/backing.img")
sectors=$(blockdev --getsz "$loop")
dmsetup create --noudevsync "$device" --table "0 $sectors flakey $loop 0 1 0"
dmsetup mknodes "$device"
mkfs.ext4 -q -F "/dev/mapper/$device"
mkdir "$workdir/mount"
mount "/dev/mapper/$device" "$workdir/mount"
umount "$workdir/mount"
if ! dmsetup reload --noudevsync "$device" --table "0 $sectors flakey $loop 0 0 1 {features}"; then
    echo 'dm-flakey does not support the requested corruption features: {features}' >&2
    exit 1
fi
"""


class DmFlakeyManager:
    """Mount node-specific, loop-backed dm-flakey storage beneath OpenEBS.

    Device-mapper and loop devices are shared by kind nodes. Both device names
    and backing-file paths therefore include the Kubernetes node UID. Avoid
    udev synchronization: a kind node has no udev daemon to acknowledge events.
    """

    def __init__(self, kubectl: KubeCtl, khaos_ns: str = DEFAULT_KHAOS_NS, khaos_label: str = DEFAULT_KHAOS_LABEL):
        self.kubectl = kubectl
        self.khaos_ns = khaos_ns
        self.khaos_label = khaos_label
        self._device_names: dict[str, str] = {}

    def device_name(self, node: str) -> str:
        if node not in self._device_names:
            data = json.loads(self.kubectl.exec_command_checked(f"kubectl get node {shlex.quote(node)} -o json"))
            uid = data["metadata"]["uid"]
            suffix = hashlib.sha256(uid.encode()).hexdigest()[:20]
            self._device_names[node] = f"{DM_FLAKEY_DEVICE_NAME}_{suffix}"
        return self._device_names[node]

    def _environment(self, node: str) -> str:
        name = self.device_name(node)
        return (
            f"DM_NAME={shlex.quote(name)}\n"
            f"BACKING_FILE={shlex.quote('/var/tmp/' + name + '.img')}\n"
            f"MOUNT_PATH={shlex.quote(OPENEBS_LOCAL_PATH)}\n"
        )

    def setup_openebs_dm_flakey_infrastructure(self, nodes: list[str] | None = None) -> None:
        if nodes is None:
            nodes = [
                node.metadata.name
                for node in self.kubectl.list_nodes().items
                if not {"node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"}.intersection(
                    node.metadata.labels or {}
                )
            ]
        if not nodes:
            raise RuntimeError("No worker nodes available for dm-flakey setup")
        attempted = []
        try:
            for node in nodes:
                attempted.append(node)
                self._setup_dm_flakey_on_node(node)
                print(f"[dm-flakey] Set up infrastructure on {node}")
            self._ensure_storage_class()
        except Exception as exc:
            # Include the failing node: it may already own a loop or dm device.
            try:
                self.teardown_openebs_dm_flakey_infrastructure(list(reversed(attempted)))
            except Exception as cleanup_exc:
                exc.add_note(f"Rollback also failed: {cleanup_exc}")
            raise

    def _ensure_storage_class(self) -> None:
        # Keep observability PVCs on the regular hostpath. Only the faulted
        # application's PVCs opt into this separate storage class.
        storage_class = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {
                "name": DM_FLAKEY_STORAGE_CLASS,
                "annotations": {
                    "openebs.io/cas-type": "local",
                    "cas.openebs.io/config": (
                        f'- name: StorageType\n  value: "hostpath"\n- name: BasePath\n  value: "{OPENEBS_LOCAL_PATH}"\n'
                    ),
                },
            },
            "provisioner": "openebs.io/local",
            "reclaimPolicy": "Delete",
            "volumeBindingMode": "WaitForFirstConsumer",
        }
        self.kubectl.exec_command_checked("kubectl apply -f -", input_data=json.dumps(storage_class))

    def _setup_dm_flakey_on_node(self, node: str) -> None:
        self._teardown_dm_flakey_on_node(node)
        script = (
            self._environment(node)
            + f"""
modprobe dm_flakey
mkdir -p "$MOUNT_PATH"
# Never format over a running workload or unrelated mount.
if mountpoint -q "$MOUNT_PATH" || [ -n "$(ls -A "$MOUNT_PATH")" ]; then
    echo "Refusing to replace nonempty or mounted storage at $MOUNT_PATH" >&2
    exit 1
fi
truncate -s {DM_FLAKEY_BACKING_FILE_SIZE_GB}G "$BACKING_FILE"
LOOP_DEV=$(losetup --find --show "$BACKING_FILE")
SECTORS=$(blockdev --getsz "$LOOP_DEV")
dmsetup create --noudevsync "$DM_NAME" --table "0 $SECTORS flakey $LOOP_DEV 0 1 0"
dmsetup mknodes "$DM_NAME"
mkfs.ext4 -q -F "/dev/mapper/$DM_NAME"
mount "/dev/mapper/$DM_NAME" "$MOUNT_PATH"
chmod 755 "$MOUNT_PATH"
"""
        )
        self._run_on_node(node, script)

    def teardown_openebs_dm_flakey_infrastructure(self, nodes: list[str] | None = None) -> None:
        if nodes is None:
            nodes = [node.metadata.name for node in self.kubectl.list_nodes().items]
        errors = []
        for node in nodes:
            try:
                self._teardown_dm_flakey_on_node(node)
                print(f"[dm-flakey] Removed infrastructure on {node}")
            except Exception as exc:
                errors.append(f"{node}: {exc}")
        if errors:
            raise RuntimeError("Failed to remove dm-flakey infrastructure: " + "; ".join(errors))

    def _teardown_dm_flakey_on_node(self, node: str) -> None:
        script = (
            self._environment(node)
            + """
if dmsetup info "$DM_NAME" >/dev/null 2>&1; then
    if mountpoint -q "$MOUNT_PATH"; then
        # Only unmount the device owned by this node. A busy device is an error,
        # not permission to force-remove storage still used by an application.
        expected=$(dmsetup info -c --noheadings -o major,minor --separator : "$DM_NAME" | tr -d '[:space:]')
        actual=$(findmnt -n -o MAJ:MIN --target "$MOUNT_PATH" | tr -d '[:space:]')
        if [ "$actual" != "$expected" ]; then
            echo "Refusing to unmount unrelated storage at $MOUNT_PATH" >&2
            exit 1
        fi
        umount "$MOUNT_PATH"
    fi
    dmsetup remove --noudevsync "$DM_NAME"
fi
if [ -f "$BACKING_FILE" ]; then
    for loop in $(losetup -j "$BACKING_FILE" | cut -d: -f1); do
        losetup -d "$loop"
    done
    rm -f "$BACKING_FILE"
fi
mkdir -p "$MOUNT_PATH"
"""
        )
        self._run_on_node(node, script)

    def _run_on_node(self, node: str, script: str) -> None:
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
            # Bound execution on the node too: a kubectl timeout alone leaves
            # the remote process alive, possibly modifying storage after cleanup.
            "timeout",
            "--kill-after=5",
            str(SETUP_TIMEOUT_SECONDS),
            "sh",
            "-ec",
            script,
        ]
        result = subprocess.run(cmd, timeout=SETUP_TIMEOUT_SECONDS + 15, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(
                f"dm-flakey operation failed on {node} (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
            )

    def _get_khaos_pod_on_node(self, node: str) -> str:
        # Re-query so a rollout cannot leave a deleted pod cached for recovery.
        out = self.kubectl.exec_command_checked(
            f"kubectl -n {shlex.quote(self.khaos_ns)} get pods -l {shlex.quote(self.khaos_label)} -o json"
        )
        for item in json.loads(out)["items"]:
            if item.get("spec", {}).get("nodeName") == node and item.get("status", {}).get("phase") == "Running":
                return item["metadata"]["name"]
        raise RuntimeError(f"No running Khaos pod found on node {node}")
