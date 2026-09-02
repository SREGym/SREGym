import json
import logging
import subprocess

from sregym.paths import BASE_DIR, PROMETHEUS_METADATA
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl


class Prometheus:
    def __init__(self):
        self.config_file = PROMETHEUS_METADATA
        self.name = None
        self.namespace = None
        self.helm_configs = {}

        self.logger = logging.getLogger("all.infra.prometheus")
        self.logger.propagate = True
        self.logger.setLevel(logging.DEBUG)

        self.load_service_json()

    def load_service_json(self):
        """Load metric service metadata into attributes."""
        with open(self.config_file) as file:
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

    def get_service_json(self) -> dict:
        """Get metric service metadata in JSON format."""
        with open(self.config_file) as file:
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
            self.logger.warning("Prometheus is already running. Skipping redeployment.")
            return

        # Wait for namespace to be fully terminated before attempting fresh install
        self._wait_for_namespace_termination()

        Helm.uninstall(**self.helm_configs)

        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.namespace)

    def teardown(self):
        """Teardown the metric collector deployment."""
        Helm.uninstall(**self.helm_configs)

    def _wait_for_namespace_termination(self):
        """Wait for namespace to be fully deleted if it's currently in Terminating state."""
        result = subprocess.run(
            f"kubectl get namespace {self.namespace} -o jsonpath='{{.status.phase}}' 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() == "Terminating":
            self.logger.info(f"Namespace '{self.namespace}' is terminating, waiting for full deletion...")
            KubeCtl().wait_for_namespace_deletion(self.namespace)

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
