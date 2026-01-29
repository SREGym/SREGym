import json
import logging
import os
import socket
import subprocess
import time

import yaml

from sregym.paths import BASE_DIR, LOKI_METADATA
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl


class Loki:
    def __init__(self):
        self.config_file = LOKI_METADATA
        self.name = None
        self.namespace = None
        self.helm_configs = {}
        self.promtail_release_name = "promtail"
        self.promtail_values_file = str(BASE_DIR / "observer/loki/promtail-values.yaml")
        self.pvc_config_file = None
        self.port = self.find_free_port()
        self.port_forward_process = None

        self.logger = logging.getLogger("all.infra.loki")
        self.logger.propagate = True
        self.logger.setLevel(logging.DEBUG)

        self.load_service_json()

    def load_service_json(self):
        """Load Loki service metadata into attributes."""
        with open(self.config_file) as file:
            metadata = json.load(file)

        self.name = metadata.get("Name")
        self.namespace = metadata.get("Namespace")

        self.helm_configs = metadata.get("Helm Config", {})

        self.name = metadata["Name"]
        self.namespace = metadata["Namespace"]
        if "Helm Config" in metadata:
            self.helm_configs = metadata["Helm Config"]
            # Handle remote charts differently - don't prepend BASE_DIR
            if not self.helm_configs.get("remote_chart", False):
                if "chart_path" in self.helm_configs:
                    chart_path = self.helm_configs["chart_path"]
                    self.helm_configs["chart_path"] = str(BASE_DIR / chart_path)

            # Resolve extra_args paths relative to BASE_DIR
            if "extra_args" in self.helm_configs:
                extra_args = self.helm_configs["extra_args"]
                resolved_args = []
                for i, arg in enumerate(extra_args):
                    if i > 0 and extra_args[i - 1] == "-f":
                        # This is a values file path, resolve it
                        resolved_args.append(str(BASE_DIR / arg))
                    else:
                        resolved_args.append(arg)
                self.helm_configs["extra_args"] = resolved_args

        self.pvc_config_file = os.path.join(BASE_DIR, metadata.get("PersistentVolumeClaimConfig"))

    def get_service_json(self) -> dict:
        """Get Loki service metadata in JSON format."""
        with open(self.config_file) as file:
            return json.load(file)

    def get_service_summary(self) -> str:
        """Get a summary of the Loki service metadata."""
        service_json = self.get_service_json()
        service_name = service_json.get("Name", "")
        namespace = service_json.get("Namespace", "")
        desc = service_json.get("Desc", "")
        supported_operations = service_json.get("Supported Operations", [])
        operations_str = "\n".join([f"  - {op}" for op in supported_operations])

        return (
            f"Telemetry Service Name: {service_name}\n"
            f"Namespace: {namespace}\n"
            f"Description: {desc}\n"
            f"Supported Operations:\n{operations_str}"
        )

    def deploy(self):
        """Deploy Loki using Helm."""
        if self._is_loki_running():
            self.logger.warning("Loki is already running. Skipping redeployment.")
            self._deploy_promtail()
            self.start_port_forward()
            return

        self._delete_pvc()
        Helm.uninstall(**self.helm_configs)

        # Add Grafana Helm repo for Loki chart
        self._add_grafana_helm_repo()

        if self.pvc_config_file:
            pvc_name = self._get_pvc_name_from_file(self.pvc_config_file)
            if not self._pvc_exists(pvc_name):
                self._apply_pvc()

        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.namespace)
        self._deploy_promtail()
        self.start_port_forward()

    def _add_grafana_helm_repo(self):
        """Add Grafana Helm repository for Loki chart."""
        self.logger.info("Adding Grafana Helm repository...")
        try:
            KubeCtl().exec_command("helm repo add grafana https://grafana.github.io/helm-charts")
            KubeCtl().exec_command("helm repo update")
        except Exception as e:
            self.logger.warning(f"Failed to add Grafana Helm repo (may already exist): {e}")

    def teardown(self):
        """Teardown the Loki deployment."""
        Helm.uninstall(**self.helm_configs)
        Helm.uninstall(release_name=self.promtail_release_name, namespace=self.namespace)

        if self.pvc_config_file:
            self._delete_pvc()
        self.stop_port_forward()

    def start_port_forward(self):
        """Starts port-forwarding to access Loki."""
        self.logger.info("Start port-forwarding for Loki.")
        if self.port_forward_process and self.port_forward_process.poll() is None:
            self.logger.warning("Port-forwarding already active.")
            return

        for attempt in range(3):
            self.logger.debug(f"Attempt {attempt + 1} of 3 in starting port-forwarding.")
            if self.is_port_in_use(self.port):
                self.logger.debug(
                    f"Port {self.port} is already in use. Attempt {attempt + 1} of 3. Retrying in 3 seconds..."
                )
                time.sleep(3)
                continue

            # Port forward to loki-gateway service on port 80
            command = f"kubectl port-forward svc/loki-gateway {self.port}:80 -n observe"
            self.port_forward_process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.environ["LOKI_PORT"] = str(self.port)
            self.logger.debug(f"Set LOKI_PORT environment variable to {self.port}")
            time.sleep(3)  # Wait a bit for the port-forward to establish

            if self.port_forward_process.poll() is None:
                self.logger.info(f"Port forwarding established at port {self.port}. LOKI_PORT set.")
                os.environ["LOKI_PORT"] = str(self.port)
                break
            else:
                self.logger.warning("Port forwarding failed. Retrying...")
        else:
            self.logger.warning("Failed to establish port forwarding after multiple attempts.")

    def stop_port_forward(self):
        """Stops the kubectl port-forward command and cleans up resources."""
        if self.port_forward_process:
            self.port_forward_process.terminate()
            try:
                self.port_forward_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.logger.warning("Port-forward process did not terminate in time, killing...")
                self.port_forward_process.kill()

            if self.port_forward_process.stdout:
                self.port_forward_process.stdout.close()
            if self.port_forward_process.stderr:
                self.port_forward_process.stderr.close()

            self.logger.info("Port forwarding for Loki stopped.")

    def is_port_in_use(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def find_free_port(self, start=32100, end=32200):
        for port in range(start, end):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return port
        raise RuntimeError("No free ports available in the range.")

    def _apply_pvc(self):
        """Apply the PersistentVolumeClaim configuration."""
        self.logger.info(f"Applying PersistentVolumeClaim from {self.pvc_config_file}")
        KubeCtl().exec_command(f"kubectl apply -f {self.pvc_config_file} -n {self.namespace}")

    def _delete_pvc(self):
        """Delete the PersistentVolume and associated PersistentVolumeClaim."""
        pvc_name = self._get_pvc_name_from_file(self.pvc_config_file)
        result = KubeCtl().exec_command(f"kubectl get pvc {pvc_name} --ignore-not-found")

        if result:
            self.logger.info(f"Deleting PersistentVolumeClaim {pvc_name}")
            KubeCtl().exec_command(f"kubectl delete pvc {pvc_name}")
            self.logger.info(f"Successfully deleted PersistentVolumeClaim from {pvc_name}")
        else:
            self.logger.warning(f"PersistentVolumeClaim {pvc_name} not found. Skipping deletion.")

    def _get_pvc_name_from_file(self, pv_config_file):
        """Extract PVC name from the configuration file."""
        with open(pv_config_file) as file:
            pv_config = yaml.safe_load(file)
            return pv_config["metadata"]["name"]

    def _pvc_exists(self, pvc_name: str) -> bool:
        """Check if the PersistentVolumeClaim exists."""
        command = f"kubectl get pvc {pvc_name}"
        try:
            result = KubeCtl().exec_command(command)
            if "No resources found" in result or "Error" in result:
                return False
        except subprocess.CalledProcessError:
            return False
        return True

    def _is_loki_running(self) -> bool:
        """Check if Loki is already running in the cluster."""
        command = f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=loki"
        try:
            result = KubeCtl().exec_command(command)
            if "Running" in result:
                return True
        except subprocess.CalledProcessError:
            return False
        return False

    def _deploy_promtail(self):
        if self._is_promtail_running():
            self.logger.warning("Promtail is already running. Skipping redeployment.")
            return

        self.logger.info("Deploying Promtail for Loki log collection...")
        Helm.install(
            release_name=self.promtail_release_name,
            chart_path="grafana/promtail",
            namespace=self.namespace,
            remote_chart=True,
            extra_args=["-f", self.promtail_values_file],
        )
        Helm.assert_if_deployed(self.namespace)

    def _is_promtail_running(self) -> bool:
        """Check if Promtail is already running in the cluster."""
        command = f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=promtail"
        try:
            result = KubeCtl().exec_command(command)
            if "Running" in result:
                return True
        except subprocess.CalledProcessError:
            return False
        return False
