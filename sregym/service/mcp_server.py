import logging
import os
import socket
import subprocess
import time

from sregym.paths import MCP_SERVER_K8S
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.sregym.mcp_server")


class MCPServer:
    def __init__(self):
        self.namespace = "sregym"
        self.service_name = "mcp-server"
        self.port = 9954
        self.port_forward_process = None
        self.kubectl = KubeCtl()

    def deploy(self):
        """Deploy the MCP server into the cluster via kustomize."""
        self.kubectl.exec_command(f"kubectl apply -k {MCP_SERVER_K8S}")
        self.kubectl.wait_for_ready(self.namespace)
        self.start_port_forward()
        logger.info("MCP server deployed successfully.")

    def is_port_in_use(self, port: int) -> bool:
        """Check if a local TCP port is already bound."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def start_port_forward(self):
        """Starts port-forwarding to access the MCP server."""
        if self.port_forward_process and self.port_forward_process.poll() is None:
            logger.warning("Port-forwarding already active.")
            return

        for attempt in range(3):
            if self.is_port_in_use(self.port):
                logger.debug(
                    f"Port {self.port} is already in use. Attempt {attempt + 1} of 3. Retrying in 3 seconds..."
                )
                time.sleep(3)
                continue

            command = f"kubectl port-forward svc/{self.service_name} {self.port}:9954 -n {self.namespace}"
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
