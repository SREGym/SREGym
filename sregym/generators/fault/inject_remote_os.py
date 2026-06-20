"""Inject faults at the OS layer via SSH (remote clusters) or docker exec (Kind)."""

import json
import os
import re
import shlex
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
            nodes = self._get_node_items()
            if nodes is None:
                return False
            self._is_kind = any((node.get("spec", {}).get("providerID") or "").startswith("kind://") for node in nodes)
        return self._is_kind

    def _get_node_items(self):
        """Return Kubernetes node objects from the current kubectl context."""
        output = self.kubectl.exec_command("kubectl get nodes -o json")
        try:
            return json.loads(output).get("items", [])
        except (json.JSONDecodeError, AttributeError, TypeError):
            print("Failed to read Kubernetes node data from kubectl.")
            return None

    def _is_control_plane_node(self, node):
        labels = node.get("metadata", {}).get("labels", {})
        return "node-role.kubernetes.io/control-plane" in labels or "node-role.kubernetes.io/master" in labels

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
        """Get Kind worker container names from the current kubectl context."""
        containers = []
        nodes = self._get_node_items()
        if nodes is None:
            return containers
        for node in nodes:
            if self._is_control_plane_node(node):
                continue
            provider_id = node.get("spec", {}).get("providerID") or ""
            if provider_id.startswith("kind://"):
                containers.append(provider_id.rsplit("/", 1)[-1])

        if not containers:
            print("No Kind worker containers found.")
        return containers

    def _get_worker_node_names(self):
        """Return list of worker node names from kubectl."""
        output = self.kubectl.exec_command("kubectl get nodes --no-headers")
        return [
            line.split()[0]
            for line in output.strip().splitlines()
            if len(line.split()) >= 3 and "control-plane" not in line.split()[2]
        ]

    def _node_exec(self, node_name: str, command: str):
        """Run a command on a remote worker node via SSH, mapping node name to inventory host."""
        worker_info = self._get_remote_worker_info()
        if not worker_info:
            print(f"No remote worker info available for {node_name}")
            return ""
        # Match node name to inventory host (inventory keys are IPs/hostnames)
        for host, user in worker_info.items():
            if node_name in host or host in node_name:
                return self._ssh_exec(host, user, f"sudo sh -c {shlex.quote(command)}")
        # Fallback: use first worker
        host, user = next(iter(worker_info.items()))
        return self._ssh_exec(host, user, f"sudo sh -c {shlex.quote(command)}")

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

    def _wait_for_single_node(
        self, node_name: str, target_status: str = "Ready", timeout: int = NODE_NOT_READY_TIMEOUT
    ):
        """Poll until a single named node reaches target status."""
        print(f"Waiting for node {node_name} to become {target_status}...")
        start = time.time()
        while time.time() - start < timeout:
            output = self.kubectl.exec_command("kubectl get nodes --no-headers")
            for line in output.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == node_name and parts[1] == target_status:
                    print(f"Node {node_name} is {target_status}.")
                    return
            time.sleep(NODE_NOT_READY_POLL_INTERVAL)
        print(f"Timed out after {timeout}s waiting for {node_name} to become {target_status}.")

    def inject_disk_pressure(
        self, node_name: str, threshold: float | None = None, margin_pct: int = 10
    ) -> float | None:
        """Raise kubelet's nodefs.available eviction threshold above the node's current free-space ratio.

        Pods evict regardless of actual disk usage. Threshold is computed dynamically from kubelet
        stats summary (current_free + margin_pct, capped at 99%) unless explicitly overridden.

        Returns the threshold percent applied (e.g. 75.0), or None if the node wasn't found.
        """
        if threshold is None:
            try:
                free_pct = self.kubectl.get_node_free_pct(node_name)

            except Exception as e:
                raise RuntimeError(
                    f"Cannot read kubelet stats summary for node {node_name} ({e!r}); "
                    f"refusing to guess a threshold — pass `threshold=` explicitly to override."
                ) from e

            threshold = float(min(99, free_pct + margin_pct))
            print(f"Node {node_name} free={free_pct}% -> threshold={threshold}%")

        value = f'"{threshold}%"'
        # Use %% to escape % in printf format string
        printf_value = value.replace("%", "%%")
        script = (
            "CFG=/var/lib/kubelet/config.yaml && "
            "if grep -q 'evictionHard:' \"$CFG\"; then "
            "  if grep -q 'nodefs.available' \"$CFG\"; then "
            f"    sed -i 's|nodefs.available:.*|nodefs.available: {value}|' \"$CFG\"; "
            "  else "
            f"    sed -i '/evictionHard:/a\\  nodefs.available: {value}' \"$CFG\"; "
            "  fi; "
            "else "
            f"  printf '\\nevictionHard:\\n  nodefs.available: {printf_value}\\n' >> \"$CFG\"; "
            "fi && "
            "systemctl restart kubelet"
        )
        if self._check_is_kind():
            containers = self._get_kind_worker_containers()
            if node_name not in containers:
                print(f"Node {node_name} not found among kind worker containers: {containers}")
                return None
            print(f"Inducing disk pressure in {node_name} (threshold {threshold}%)...")
            self._docker_exec(node_name, script)
        else:
            worker_nodes = self._get_worker_node_names()
            if node_name not in worker_nodes:
                print(f"Node {node_name} not found among worker nodes: {worker_nodes}")
                return None
            print(f"Inducing disk pressure on {node_name} (threshold {threshold}%)...")
            self._node_exec(node_name, script)

        self._wait_for_single_node(node_name, target_status="Ready")
        return threshold

    def recover_disk_pressure(self, node_name: str):
        """Restore the kubelet eviction threshold and restart kubelet."""
        script = "CFG=/var/lib/kubelet/config.yaml && sed -i '/nodefs.available:/d' \"$CFG\"; systemctl restart kubelet"
        if self._check_is_kind():
            containers = self._get_kind_worker_containers()
            if node_name not in containers:
                print(f"Node {node_name} not found among kind worker containers: {containers}")
                return
            print(f"Recovering disk pressure in {node_name}...")
            self._docker_exec(node_name, script)
        else:
            worker_nodes = self._get_worker_node_names()
            if node_name not in worker_nodes:
                print(f"Node {node_name} not found among worker nodes: {worker_nodes}")
                return
            print(f"Recovering disk pressure on {node_name}...")
            self._node_exec(node_name, script)

        self._wait_for_single_node(node_name, target_status="Ready")

    def recover_disk_pressure_all(self):
        """Strip the nodefs.available eviction threshold on every worker node."""
        if self._check_is_kind():
            nodes = self._get_kind_worker_containers()
        else:
            if not self._check_remote_host():
                return
            nodes = self._get_worker_node_names()
        for node_name in nodes:
            self.recover_disk_pressure(node_name)


def main():
    injector = RemoteOSFaultInjector()
    print("Injecting kubelet crash...")
    injector.inject_kubelet_crash()
    input("Press Enter to recover...")
    print("Recovering...")
    injector.recover_kubelet_crash()


if __name__ == "__main__":
    main()
