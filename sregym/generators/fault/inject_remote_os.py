"""Inject faults at the OS layer via kubectl debug node (remote clusters) or docker exec (Kind)."""

import subprocess
import time

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl

NODE_NOT_READY_TIMEOUT = 120  # seconds
NODE_NOT_READY_POLL_INTERVAL = 5  # seconds


class RemoteOSFaultInjector(FaultInjector):
    def __init__(self):
        self.kubectl = KubeCtl()
        self._is_kind = None

    def _check_is_kind(self):
        """Detect if the cluster is Kind-based."""
        if self._is_kind is None:
            out = self.kubectl.exec_command("kubectl get nodes")
            self._is_kind = "kind-worker" in out
        return self._is_kind

    def _get_worker_node_names(self) -> list[str]:
        """Return the names of all worker (non-control-plane) nodes via kubectl."""
        output = self.kubectl.exec_command("kubectl get nodes --no-headers")
        names = []
        for line in output.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3 and "control-plane" not in parts[2]:
                names.append(parts[0])
        return names

    def _node_exec(self, node_name: str, command: str):
        """Run a command on a node's host OS using ``kubectl debug node``.

        Creates an ephemeral privileged container, runs the command via
        ``chroot /host``, and cleans up automatically (--rm).
        """
        cmd = f"kubectl debug node/{node_name} -it --rm --image=busybox -- chroot /host sh -c {_shell_quote(command)}"
        return self.kubectl.exec_command(cmd)

    def _docker_exec(self, container: str, command: str):
        """Run a command inside a Docker container (for Kind nodes)."""
        result = subprocess.run(
            ["docker", "exec", container, "bash", "-c", command],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"docker exec failed on {container}: {result.stderr.strip()}")
        return result.stdout

    def _get_kind_worker_containers(self):
        """Get Kind worker container names."""
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=kind-worker", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Failed to list Kind containers: {result.stderr.strip()}")
            return []
        containers = [c.strip() for c in result.stdout.strip().splitlines() if c.strip()]
        if not containers:
            print("No Kind worker containers found.")
        return containers

    def _wait_for_worker_nodes(self, target_status="NotReady", timeout=NODE_NOT_READY_TIMEOUT):
        """Poll until all worker nodes reach the target status ('Ready' or 'NotReady')."""
        worker_node_names = set(self._get_worker_node_names())

        if not worker_node_names:
            print("No worker nodes found in cluster.")
            return

        print(f"Waiting for worker nodes {worker_node_names} to become {target_status}...")
        start = time.time()
        while time.time() - start < timeout:
            output = self.kubectl.exec_command("kubectl get nodes --no-headers")
            all_matched = True
            for line in output.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] in worker_node_names:
                    if parts[1] != target_status:
                        all_matched = False
                        break
            if all_matched:
                print(f"All worker nodes are {target_status}.")
                return
            time.sleep(NODE_NOT_READY_POLL_INTERVAL)

        print(f"Timed out after {timeout}s waiting for nodes to become {target_status}.")

    def inject_kubelet_crash(self):
        """Force-kill kubelet and stop the service on all worker nodes."""
        if self._check_is_kind():
            containers = self._get_kind_worker_containers()
            if not containers:
                return
            for container in containers:
                print(f"Killing kubelet in {container}...")
                self._docker_exec(container, "kill -9 $(pgrep -x kubelet) 2>/dev/null; systemctl stop kubelet")
                print(f"Kubelet stopped in {container}")
        else:
            worker_nodes = self._get_worker_node_names()
            if not worker_nodes:
                print("No worker nodes found.")
                return
            for node in worker_nodes:
                print(f"Killing kubelet on {node}...")
                self._node_exec(node, "kill -9 $(pgrep -x kubelet) 2>/dev/null; systemctl stop kubelet")
                print(f"Kubelet stopped on {node}")

        self._wait_for_worker_nodes("NotReady")

    def recover_kubelet_crash(self):
        """Restart kubelet on all worker nodes."""
        if self._check_is_kind():
            containers = self._get_kind_worker_containers()
            if not containers:
                return
            for container in containers:
                print(f"Starting kubelet in {container}...")
                self._docker_exec(container, "systemctl start kubelet")
                print(f"Kubelet started in {container}")
        else:
            worker_nodes = self._get_worker_node_names()
            if not worker_nodes:
                print("No worker nodes found.")
                return
            for node in worker_nodes:
                print(f"Starting kubelet on {node}...")
                self._node_exec(node, "systemctl start kubelet")
                print(f"Kubelet started on {node}")

        self._wait_for_worker_nodes("Ready")


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe shell interpolation."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def main():
    injector = RemoteOSFaultInjector()
    print("Injecting kubelet crash...")
    injector.inject_kubelet_crash()
    input("Press Enter to recover...")
    print("Recovering...")
    injector.recover_kubelet_crash()


if __name__ == "__main__":
    main()
