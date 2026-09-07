import logging
import os
import socket
import subprocess
import time

import requests
import yaml

from sregym.paths import MCP_SERVER_K8S
from sregym.service.kubectl import KubeCtl
from sregym.service.kubernetes_access_policy import restricted_cluster_role

logger = logging.getLogger("all.sregym.mcp_server")


class MCPServer:
    def __init__(self, *, restrict_network_access: bool = False):
        self.namespace = "sregym"
        self.service_name = "mcp-server"
        self.port = 9954
        self.port_forward_process = None
        self.kubectl = KubeCtl()
        self.restrict_network_access = restrict_network_access

    def _is_running(self) -> bool:
        """Check if the MCP server deployment already exists and is ready."""
        result = self.kubectl.exec_command(
            f"kubectl get deployment {self.service_name} -n {self.namespace} -o jsonpath='{{.status.readyReplicas}}'"
        )
        value = result.strip().strip("'")
        # exec_command returns stderr on failure (e.g. "Error from server (NotFound)"),
        # so only treat a purely numeric positive value as "running".
        return value.isdigit() and int(value) > 0

    def _network_environment(self) -> dict[str, str]:
        value = str(self.restrict_network_access).lower()
        return {
            "RESTRICT_NETWORK_ACCESS": value,
            # Older MCP images read this name. Keep it synchronized during upgrades.
            "BLOCK_WORKLOAD_CREATION": value,
        }

    def _deployment_resources(self) -> list[dict]:
        """Configure the rendered manifests before any resource is applied."""
        rendered = self.kubectl.exec_command_checked(f"kubectl kustomize {MCP_SERVER_K8S}", timeout=30)
        resources = [body for body in yaml.safe_load_all(rendered) if body]
        for index, body in enumerate(resources):
            if body["kind"] == "ClusterRole" and self.restrict_network_access:
                resources[index] = restricted_cluster_role(body)
            elif body["kind"] == "Deployment" and body["metadata"]["name"] == self.service_name:
                for container in body["spec"]["template"]["spec"]["containers"]:
                    if container["name"] == self.service_name:
                        network_env = self._network_environment()
                        container["env"] = [
                            entry for entry in container.get("env", []) if entry["name"] not in network_env
                        ] + [{"name": name, "value": value} for name, value in network_env.items()]
        return resources

    def deploy(self):
        """Apply the selected mode and wait for readiness before exposing MCP."""
        resources = self._deployment_resources()
        already_running = self._is_running()
        if already_running:
            # Keep the existing image and other Deployment customizations.
            resources = [body for body in resources if body["kind"] in {"ClusterRole", "ClusterRoleBinding"}]

        self.kubectl.exec_command_checked("kubectl apply -f -", input_data=yaml.safe_dump_all(resources), timeout=60)
        if already_running:
            env_args = " ".join(f"{name}={value}" for name, value in self._network_environment().items())
            self.kubectl.exec_command_checked(
                f"kubectl set env deployment/{self.service_name} -n {self.namespace} {env_args}", timeout=30
            )

        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.service_name} -n {self.namespace} --timeout=180s",
            timeout=190,
        )
        if not already_running:
            self.kubectl.wait_for_ready(self.namespace)
        if not self._is_port_forward_healthy():
            self.start_port_forward()
        logger.info("MCP server deployment and access policy are ready.")

    def is_port_in_use(self, port: int) -> bool:
        """Check if a local TCP port is already bound."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _is_port_forward_healthy(self) -> bool:
        """Check if the port-forward is actually serving traffic, not just bound."""
        try:
            resp = requests.get(f"http://127.0.0.1:{self.port}/kubectl/sse", stream=True, timeout=5)
            resp.close()
            # SSE endpoint returns 200 with text/event-stream
            return resp.status_code == 200
        except Exception:
            return False

    def _kill_stale_port_forward(self):
        """Kill any existing kubectl port-forward process on our port."""
        if self.port_forward_process and self.port_forward_process.poll() is None:
            logger.info("Killing existing port-forward process to re-establish fresh connection.")
            self.stop_port_forward()
            self.port_forward_process = None

        # Also kill orphaned port-forward processes from previous runs that we
        # don't hold a handle to (e.g. the process survived a previous crash).
        if self.is_port_in_use(self.port):
            try:
                result = subprocess.run(f"lsof -ti tcp:{self.port}", shell=True, capture_output=True, text=True)
                for pid in result.stdout.strip().split():
                    if pid.isdigit():
                        logger.info(f"Killing orphaned process {pid} on port {self.port}")
                        subprocess.run(f"kill {pid}", shell=True)
                time.sleep(1)
            except Exception as e:
                logger.warning(f"Failed to kill stale port-forward: {e}")

    def start_port_forward(self):
        """Starts port-forwarding to access the MCP server."""
        self._kill_stale_port_forward()

        for attempt in range(3):
            if self.is_port_in_use(self.port):
                logger.debug(
                    f"Port {self.port} is already in use. Attempt {attempt + 1} of 3. Retrying in 3 seconds..."
                )
                time.sleep(3)
                continue

            command = (
                f"kubectl port-forward svc/{self.service_name} {self.port}:9954 -n {self.namespace} --address 0.0.0.0"
            )
            self.port_forward_process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(3)

            if self.port_forward_process.poll() is None:
                os.environ["MCP_SERVER_PORT"] = str(self.port)
                logger.info(f"Port forwarding established at {self.port}. MCP_SERVER_PORT set.")
                break
            else:
                logger.warning("Port forwarding failed. Retrying...")
        else:
            logger.warning("Failed to establish port forwarding after multiple attempts.")

    def stop_port_forward(self):
        """Stops the kubectl port-forward command and cleans up resources."""
        if self.port_forward_process:
            self.port_forward_process.terminate()
            try:
                self.port_forward_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Port-forward process did not terminate in time, killing...")
                self.port_forward_process.kill()

            if self.port_forward_process.stdout:
                self.port_forward_process.stdout.close()
            if self.port_forward_process.stderr:
                self.port_forward_process.stderr.close()

            logger.info("Port forwarding for MCP server stopped.")
