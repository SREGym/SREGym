import json
import os
import socket
import subprocess
import threading
import time

import yaml

from srearena.paths import BASE_DIR, PROMETHEUS_METADATA
from srearena.service.helm import Helm
from srearena.service.kubectl import KubeCtl


class Prometheus:
    def __init__(self):
        self.config_file = PROMETHEUS_METADATA
        self.name = None
        self.namespace = None
        self.helm_configs = {}
        self.pvc_config_file = None
        self.port = self.find_free_port()
        self.port_forward_process = None

        self.load_service_json()

    def load_service_json(self):
        """Load metric service metadata into attributes."""
        with open(self.config_file, "r") as file:
            metadata = json.load(file)

        self.name = metadata.get("Name")
        self.namespace = metadata.get("Namespace")

        self.helm_configs = metadata.get("Helm Config", {})

        self.name = metadata["Name"]
        self.namespace = metadata["Namespace"]
        if "Helm Config" in metadata:
            self.helm_configs = metadata["Helm Config"]
            if "chart_path" in self.helm_configs:
                chart_path = self.helm_configs["chart_path"]
                self.helm_configs["chart_path"] = str(BASE_DIR / chart_path)

        self.pvc_config_file = os.path.join(BASE_DIR, metadata.get("PersistentVolumeClaimConfig"))

    def get_service_json(self) -> dict:
        """Get metric service metadata in JSON format."""
        with open(self.config_file, "r") as file:
            return json.load(file)

    def get_service_summary(self) -> str:
        """Get a summary of the metric service metadata."""
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
        """Deploy the metric collector using Helm."""
        if self._is_prometheus_running():
            print("Prometheus is already running. Skipping redeployment.")
            return

        self._delete_pvc()
        Helm.uninstall(**self.helm_configs)

        if self.pvc_config_file:
            pvc_name = self._get_pvc_name_from_file(self.pvc_config_file)
            if not self._pvc_exists(pvc_name):
                self._apply_pvc()

        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.namespace)
        self.start_port_forward()

    def teardown(self):
        """Teardown the metric collector deployment."""
        Helm.uninstall(**self.helm_configs)

        if self.pvc_config_file:
            self._delete_pvc()
        self.stop_port_forward()

    def start_port_forward(self):
        """Starts port-forwarding to access Prometheus."""
        print("Start port-forwarding for Prometheus.")
        if self.port_forward_process and self.port_forward_process.poll() is None:
            print("Port-forwarding already active.")
            return

        for attempt in range(3):
            if self.is_port_in_use(self.port):
                print(f"Port {self.port} is already in use. Attempt {attempt + 1} of 3. Retrying in 3 seconds...")
                time.sleep(3)
                continue

            command = f"kubectl port-forward svc/prometheus-server {self.port}:80 -n observe"
            self.port_forward_process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.environ["PROMETHEUS_PORT"] = str(self.port)
            time.sleep(3)  # Wait a bit for the port-forward to establish

            if self.port_forward_process.poll() is None:
                print(f"Port forwarding established at {self.port}.")
                os.environ["PROMETHEUS_PORT"] = str(self.port)
                break
            else:
                print("Port forwarding failed. Retrying...")
        else:
            print("Failed to establish port forwarding after multiple attempts.")

    def stop_port_forward(self):
        """Stops the kubectl port-forward command and cleans up resources."""
        if self.port_forward_process:
            self.port_forward_process.terminate()
            try:
                self.port_forward_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("Port-forward process did not terminate in time, killing...")
                self.port_forward_process.kill()

            if self.port_forward_process.stdout:
                self.port_forward_process.stdout.close()
            if self.port_forward_process.stderr:
                self.port_forward_process.stderr.close()

            print("Port forwarding stopped.")

    def is_port_in_use(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def find_free_port(self, start=32000, end=32100):
        for port in range(start, end):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return port
        raise RuntimeError("No free ports available in the range.")

    def _apply_pvc(self):
        """Apply the PersistentVolumeClaim configuration."""
        print(f"Applying PersistentVolumeClaim from {self.pvc_config_file}")
        KubeCtl().exec_command(f"kubectl apply -f {self.pvc_config_file} -n {self.namespace}")

    def _delete_pvc(self):
        """Delete the PersistentVolume and associated PersistentVolumeClaim."""
        pvc_name = self._get_pvc_name_from_file(self.pvc_config_file)
        result = KubeCtl().exec_command(f"kubectl get pvc {pvc_name} --ignore-not-found")

        if result:
            print(f"Deleting PersistentVolumeClaim {pvc_name}")
            KubeCtl().exec_command(f"kubectl delete pvc {pvc_name}")
            print(f"Successfully deleted PersistentVolumeClaim from {pvc_name}")
        else:
            print(f"PersistentVolumeClaim {pvc_name} not found. Skipping deletion.")

    def _get_pvc_name_from_file(self, pv_config_file):
        """Extract PVC name from the configuration file."""
        with open(pv_config_file, "r") as file:
            pv_config = yaml.safe_load(file)
            return pv_config["metadata"]["name"]

    def _pvc_exists(self, pvc_name: str) -> bool:
        """Check if the PersistentVolumeClaim exists."""
        command = f"kubectl get pvc {pvc_name}"
        try:
            result = KubeCtl().exec_command(command)
            if "No resources found" in result or "Error" in result:
                return False
        except subprocess.CalledProcessError as e:
            return False
        return True

    def _is_prometheus_running(self) -> bool:
        """Check if Prometheus is already running in the cluster."""
        command = f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=prometheus"
        try:
            result = KubeCtl().exec_command(command)
            if "Running" in result:
                return True
        except subprocess.CalledProcessError:
            return False
        return False
        
