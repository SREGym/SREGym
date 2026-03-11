import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("all.sregym.otel_collector")


class OtelCollector:
    def __init__(self):
        self.namespace = "observe"
        base_dir = Path(__file__).parent
        self.config_file = base_dir / "otel-collector.yaml"

    def run_cmd(self, cmd: str) -> str:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Command failed: {cmd}\nError: {result.stderr}")
        return result.stdout.strip()

    def deploy(self):
        """Deploy OTel Collector with spanmetrics connector."""
        self._create_jaeger_backend_service()
        self.run_cmd(f"kubectl apply -f {self.config_file} -n {self.namespace}")
        self._wait_for_ready(timeout=120)
        logger.info("OTel Collector deployed successfully.")

    def _create_jaeger_backend_service(self):
        """Create a jaeger-backend service pointing to the Jaeger pod.

        The OTel Collector exports traces to jaeger-backend:4317 (OTLP).
        This keeps the original jaeger-agent service name free for the
        ExternalName redirect in app namespaces.
        """
        self.run_cmd(f"kubectl -n {self.namespace} delete svc jaeger-backend --ignore-not-found")
        manifest = (
            '{"apiVersion":"v1","kind":"Service","metadata":{"name":"jaeger-backend",'
            f'"namespace":"{self.namespace}"'
            '},"spec":{"ports":[{"port":4317,"name":"otlp-grpc"},{"port":16686,"name":"ui"}],'
            '"selector":{"app-name":"jaeger"}}}'
        )
        self.run_cmd(f"echo '{manifest}' | kubectl apply -f -")

    def _wait_for_ready(self, timeout: int = 120):
        """Wait until the OTel Collector pod is ready."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                out = self.run_cmd(
                    f"kubectl -n {self.namespace} get deployment otel-collector -o jsonpath='{{.status.readyReplicas}}'"
                )
                if out.strip("'") == "1":
                    return
            except Exception:
                pass
            time.sleep(3)
        raise RuntimeError(f"OTel Collector not ready within {timeout}s")
