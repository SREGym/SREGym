import json
import shlex
import time
from collections.abc import Iterable

from sregym.paths import KHAOS_DS
from sregym.service.khaos_capabilities import KhaosCapability
from sregym.service.kubectl import KubeCtl

KHAOS_NS = "khaos"
KHAOS_DS_NAME = "khaos"


class KhaosUnsupportedError(RuntimeError):
    """Raised when a node kernel cannot provide a problem's Khaos capability."""


class KhaosController:
    def __init__(self, kubectl: KubeCtl):
        self.kubectl = kubectl

    def ensure_deployed(self, required_capabilities: Iterable[KhaosCapability] = ()):
        self.kubectl.exec_command_checked(f"kubectl get ns {KHAOS_NS} >/dev/null 2>&1 || kubectl create ns {KHAOS_NS}")
        self.kubectl.exec_command_checked(f"kubectl apply -f {KHAOS_DS}")

        # Wait for both DaemonSets to be ready (control-plane and worker)
        # The YAML file contains two DaemonSets: khaos-control-plane and khaos-worker
        self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} rollout status ds/khaos-control-plane --timeout=3m")
        self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} rollout status ds/khaos-worker --timeout=3m")
        self._check_capabilities(frozenset(required_capabilities))

    def _running_pods(self, profile: str | None = None) -> list[dict]:
        selector = "app=khaos"
        if profile:
            selector += f",profile={profile}"
        out = self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} get pods -l {shlex.quote(selector)} -o json")
        data = json.loads(out or "{}")
        return [item for item in data.get("items", []) if item.get("status", {}).get("phase") == "Running"]

    def _check_capabilities(self, required_capabilities: frozenset[KhaosCapability]) -> None:
        for capability in sorted(required_capabilities, key=str):
            profile = "worker" if capability == KhaosCapability.DM_FLAKEY else None
            pods = self._running_pods(profile=profile)
            if not pods:
                target = "worker nodes" if profile else "cluster nodes"
                raise KhaosUnsupportedError(f"Khaos capability {capability.value!r} has no running pod on {target}")

            for pod in pods:
                pod_name = pod["metadata"]["name"]
                node_name = pod.get("spec", {}).get("nodeName", "unknown")
                try:
                    self._check_pod_capability(pod_name, capability)
                except RuntimeError as exc:
                    raise KhaosUnsupportedError(
                        f"Node {node_name!r} does not support Khaos capability {capability.value!r}: {exc}"
                    ) from exc

    def _check_pod_capability(self, pod_name: str, capability: KhaosCapability) -> None:
        pod = shlex.quote(pod_name)
        if capability == KhaosCapability.EBPF_SYSCALL:
            self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} exec {pod} -- /khaos/khaos --check")
            return

        if capability == KhaosCapability.DM_FLAKEY:
            script = """
set -e
command -v dmsetup >/dev/null
command -v losetup >/dev/null
command -v mkfs.ext4 >/dev/null
modprobe dm_flakey
dmsetup targets | grep -qw flakey
""".strip()
            self.kubectl.exec_command_checked(
                f"kubectl -n {KHAOS_NS} exec {pod} -- nsenter -t 1 -m -u -i -n -p sh -ec {shlex.quote(script)}"
            )
            return

        raise KhaosUnsupportedError(f"Unknown Khaos capability: {capability}")

    def teardown(self):
        self.kubectl.exec_command(f"kubectl delete ns {KHAOS_NS} --ignore-not-found")

    def _khaos_pod_on_node(self, node_name: str) -> str:
        deadline = time.time() + 90
        while time.time() < deadline:
            out = self.kubectl.exec_command(f"kubectl -n {KHAOS_NS} get pods -o json")
            if isinstance(out, tuple):
                out = out[0]
            data = json.loads(out or "{}")
            for item in data.get("items", []):
                if (
                    item.get("spec", {}).get("nodeName") == node_name
                    and item.get("status", {}).get("phase") == "Running"
                ):
                    return item["metadata"]["name"]
            time.sleep(3)
        # diagnostics
        ds = self.kubectl.exec_command(f"kubectl -n {KHAOS_NS} get ds -o wide")
        pods = self.kubectl.exec_command(f"kubectl -n {KHAOS_NS} get pods -o wide")
        raise RuntimeError(
            f"No running Khaos pod on node {node_name} after 90s.\n"
            f"DaemonSets:\n{ds[0] if isinstance(ds, tuple) else ds}\n"
            f"Pods:\n{pods[0] if isinstance(pods, tuple) else pods}"
        )

    def inject(self, node_name: str, fault_name: str, host_pid: int):
        """
        Run:  /khaos/khaos <fault_name> <pid>
        inside the Khaos pod on the specified node.
        """
        pod = self._khaos_pod_on_node(node_name)
        cmd = f"kubectl -n {KHAOS_NS} exec {pod} -- /khaos/khaos {fault_name} {host_pid}"
        out = self.kubectl.exec_command(cmd)
        return out

    def recover(self, node_name: str, fault_name: str):
        pod = self._khaos_pod_on_node(node_name)
        cmd = f"kubectl -n {KHAOS_NS} exec {pod} -- /khaos/khaos --recover {fault_name}"
        out = self.kubectl.exec_command(cmd)
        return out
