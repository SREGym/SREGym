import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("all.sregym.jaeger")


class Jaeger:
    def __init__(self):
        self.namespace = "observe"
        base_dir = Path(__file__).parent
        self.config_file = base_dir / "jaeger.yaml"

    def run_cmd(self, cmd: str) -> str:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Command failed: {cmd}\nError: {result.stderr}")
        return result.stdout.strip()

    def deploy(self):
        """Deploy Jaeger with TiDB as the storage backend."""
        self.run_cmd(f"kubectl apply -f {self.config_file} -n {self.namespace}")
        self.wait_for_service("jaeger-out", timeout=120)
        logger.info("Jaeger deployed successfully.")

    def wait_for_service(self, service: str, timeout: int = 60):
        """Wait until the Jaeger service exists in Kubernetes."""
        print(f"[debug] waiting for service {service} in ns={self.namespace}")
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                self.run_cmd(f"kubectl -n {self.namespace} get svc {service}")
                print(f"[debug] found service {service}")
                return
            except Exception:
                time.sleep(3)
        raise RuntimeError(f"Service {service} not found within {timeout}s")

    def create_external_name_service(self, namespace: str, service_name: str = "jaeger"):
        """Create an ExternalName service in the app namespace that redirects
        Jaeger traffic to the centralized Jaeger in the observe namespace.

        This replaces any app-local Jaeger deployment/service so traces flow
        to the shared observability stack.
        """
        # Delete any app-local Jaeger deployment
        for resource in ["deployment", "statefulset"]:
            self.run_cmd(f"kubectl delete {resource} -n {namespace} -l app-name=jaeger --ignore-not-found")
            self.run_cmd(f"kubectl delete {resource} -n {namespace} {service_name} --ignore-not-found")

        # Delete existing Jaeger service in the app namespace
        self.run_cmd(f"kubectl delete svc -n {namespace} {service_name} --ignore-not-found")

        # Create ExternalName service pointing to observe namespace
        external_name = f"jaeger-agent.{self.namespace}.svc.cluster.local"
        self.run_cmd(
            f"kubectl create service externalname {service_name} -n {namespace} --external-name {external_name}"
        )

        logger.info(f"Created ExternalName service '{service_name}' in namespace '{namespace}' -> {external_name}")
