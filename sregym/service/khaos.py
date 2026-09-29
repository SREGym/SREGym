import json
import shlex
import time

from sregym.paths import KHAOS_DS
from sregym.service.kubectl import KubeCtl

KHAOS_NS = "khaos"
KHAOS_DS_NAME = "khaos"


class KhaosUnsupportedError(RuntimeError):
    """Raised when a node cannot run Khaos eBPF faults."""


class KhaosController:
    def __init__(self, kubectl: KubeCtl):
        self.kubectl = kubectl

    def ensure_deployed(self):
        self.kubectl.exec_command_checked(f"kubectl get ns {KHAOS_NS} >/dev/null 2>&1 || kubectl create ns {KHAOS_NS}")
        self.kubectl.exec_command_checked(f"kubectl apply -f {KHAOS_DS}")

        # Wait for both DaemonSets to be ready (control-plane and worker)
        # The YAML file contains two DaemonSets: khaos-control-plane and khaos-worker
        self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} rollout status ds/khaos-control-plane --timeout=3m")
        self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} rollout status ds/khaos-worker --timeout=3m")
        self._check_ebpf_support()

    def _check_ebpf_support(self) -> None:
        out = self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} get pods -l app=khaos -o json")
        pods = [item for item in json.loads(out or "{}").get("items", []) if item["status"].get("phase") == "Running"]
        if not pods:
            raise KhaosUnsupportedError("no Khaos pod is running on any node")

        for pod in pods:
            node_name = pod["spec"].get("nodeName", "unknown")
            try:
                self.kubectl.exec_command_checked(
                    f"kubectl -n {KHAOS_NS} exec {shlex.quote(pod['metadata']['name'])} -- /khaos/khaos --check"
                )
            except RuntimeError as exc:
                raise KhaosUnsupportedError(f"Node {node_name!r} cannot run Khaos eBPF faults: {exc}") from exc

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
