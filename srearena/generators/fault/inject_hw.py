import subprocess

from srearena.generators.fault.base import FaultInjector
from srearena.service.kubectl import KubeCtl


class HWFaultInjector(FaultInjector):
    def __init__(self):
        self.kubectl = KubeCtl()
        self.khaos_daemonset_label = "app=khaos"

    def _inject(self, microservices: list[str], fault_type: str):
        for svc in microservices:
            node = self.kubectl.get_node_of_pod(svc)
            pid = self._get_pid_in_container(svc)
            self._exec_khaos_fault_on_node(node, fault_type, pid)

    def _get_pid_in_container(self, pod_name: str) -> int:
        # Assumes single-container pods. You might want to expand this.
        pid_cmd = f"kubectl exec {pod_name} -- pidof main"
        pid = subprocess.check_output(pid_cmd.split()).decode().strip()
        return int(pid)

    def _exec_khaos_fault_on_node(self, node: str, fault_type: str, pid: int):
        # Find the DaemonSet pod on the correct node
        ds_pods = self.kubectl.get_pods_by_label(self.khaos_daemonset_label)
        target_pod = next(pod for pod in ds_pods if pod["spec"]["nodeName"] == node)

        inject_cmd = f"./khaos {fault_type} {pid}"
        full_cmd = ["kubectl", "exec", target_pod["metadata"]["name"], "--", "bash", "-c", inject_cmd]
        subprocess.run(full_cmd, check=True)

    # Example hardware fault
    def read_error(self, microservices: list[str]):
        return self._inject(microservices, "read_error")
