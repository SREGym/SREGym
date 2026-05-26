"""Inject faults at the OS layer via SSH (remote clusters) or docker exec (Kind)."""

import os
import re
import subprocess
import time

import paramiko
import yaml
from paramiko.client import AutoAddPolicy

from sregym.generators.fault.base import FaultInjector
from sregym.paths import BASE_DIR
from sregym.service.kubectl import KubeCtl

NODE_NOT_READY_TIMEOUT = 120  # seconds
NODE_NOT_READY_POLL_INTERVAL = 5  # seconds


class RemoteOSFaultInjector(FaultInjector):
    def __init__(self):
        self.kubectl = KubeCtl()
        self.worker_info = None
        self._is_kind = None

    def _check_is_kind(self):
        """Detect if the cluster is Kind-based."""
        if self._is_kind is None:
            out = self.kubectl.exec_command("kubectl get nodes")
            self._is_kind = "kind-worker" in out
        return self._is_kind

    def _check_remote_host(self):
        """Verify the remote cluster has an inventory file."""
        if not os.path.exists(f"{BASE_DIR}/../scripts/ansible/inventory.yml"):
            print("Inventory file not found: " + f"{BASE_DIR}/../scripts/ansible/inventory.yml")
            return False
        return True

    def _get_remote_worker_info(self):
        """Read worker node SSH info from the Ansible inventory."""
        if self.worker_info:
            return self.worker_info

        worker_info = {}
        with open(f"{BASE_DIR}/../scripts/ansible/inventory.yml") as f:
            inventory = yaml.safe_load(f)

        variables = inventory.get("all", {}).get("vars", {})
        children = inventory.get("all", {}).get("children", {})
        workers = children.get("worker_nodes", {}).get("hosts", {})

        if not workers:
            print("No worker nodes found in inventory.")
            return None

        for name, info in workers.items():
            host = info["ansible_host"]
            user = self._replace_variables(info["ansible_user"], variables)
            if "{{" in user:
                print(f"Warning: Unresolved variables in {name} user: {user}")
                continue
            worker_info[host] = user

        self.worker_info = worker_info
        return self.worker_info

    def _replace_variables(self, text: str, variables: dict) -> str:
        """Replace {{ variable_name }} with actual values from variables dict."""

        def replace_var(match):
            var_name = match.group(1).strip()
            return str(variables[var_name]) if var_name in variables else match.group(0)

        return re.sub(r"\{\{\s*(\w+)\s*\}\}", replace_var, text)

    def _ssh_exec(self, host: str, user: str, command: str):
        """Run a command on a remote host via SSH."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(AutoAddPolicy())
        try:
            ssh.connect(host, username=user)
            stdin, stdout, stderr = ssh.exec_command(command)
            stdout.channel.recv_exit_status()
            return stdout.read().decode()
        finally:
            ssh.close()

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
        output = self.kubectl.exec_command("kubectl get nodes --no-headers")
        worker_node_names = set()
        for line in output.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3 and "control-plane" not in parts[2]:
                worker_node_names.add(parts[0])

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
            if not self._check_remote_host():
                return
            worker_info = self._get_remote_worker_info()
            if not worker_info:
                return
            for host, user in worker_info.items():
                print(f"Killing kubelet on {host}...")
                self._ssh_exec(host, user, "sudo kill -9 $(pgrep -x kubelet) 2>/dev/null; sudo systemctl stop kubelet")
                print(f"Kubelet stopped on {host}")

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
            if not self._check_remote_host():
                return
            worker_info = self._get_remote_worker_info()
            if not worker_info:
                return
            for host, user in worker_info.items():
                print(f"Starting kubelet on {host}...")
                self._ssh_exec(host, user, "sudo systemctl start kubelet")
                print(f"Kubelet started on {host}")

        self._wait_for_worker_nodes("Ready")


def main():
    injector = RemoteOSFaultInjector()
    print("Injecting kubelet crash...")
    injector.inject_kubelet_crash()
    input("Press Enter to recover...")
    print("Recovering...")
    injector.recover_kubelet_crash()


if __name__ == "__main__":
    main()
