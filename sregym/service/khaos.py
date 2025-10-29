import json
import time

from sregym.paths import KHAOS_DS
from sregym.service.kubectl import KubeCtl

KHAOS_NS = "khaos"
KHAOS_DS_NAME = "khaos"


class KhaosController:
    def __init__(self, kubectl: KubeCtl):
        self.kubectl = kubectl

    def ensure_deployed(self):
        if self.kubectl.is_emulated_cluster():
            raise RuntimeError("Khaos cannot be deployed on emulated clusters (kind, minikube, k3d, etc.).")

        rc = self.kubectl.exec_command(f"kubectl get ns {KHAOS_NS} >/dev/null 2>&1 || kubectl create ns {KHAOS_NS}")

        cmd = f"kubectl apply -f {KHAOS_DS}"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            stdout, stderr, rc = (out + ("",))[:3]
            if rc not in (0, None):
                raise RuntimeError(f"kubectl apply failed (rc={rc}).\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")

        self.kubectl.exec_command(f"kubectl -n {KHAOS_NS} rollout status ds/{KHAOS_DS_NAME} --timeout=3m")

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
