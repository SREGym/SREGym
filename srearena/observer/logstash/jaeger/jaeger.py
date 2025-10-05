import os
import socket
import subprocess
import time
from pathlib import Path


class Jaeger:
    def __init__(self):
        self.namespace = "observe"
        base_dir = Path(__file__).parent
        self.config_file = base_dir / "jaeger.yaml"
        self.port = 16686  # local port for Jaeger UI
        self.port_forward_process = None
        os.environ["JAEGER_BASE_URL"] = f"http://localhost:{self.port}"

    def run_cmd(self, cmd: str) -> str:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Command failed: {cmd}\nError: {result.stderr}")
        return result.stdout.strip()

    def deploy(self):
        """Deploy Jaeger with TiDB as the storage backend."""
        self.run_cmd(f"kubectl apply -f {self.config_file} -n {self.namespace}")
        self.wait_for_service("jaeger-out", timeout=120)
        self.start_port_forward()
        print("Jaeger deployed successfully.")

    def is_port_in_use(self, port: int) -> bool:
        """Check if a local TCP port is already bound."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def wait_for_service(self, service: str, timeout: int = 60):
        """Wait until the Jaeger service exists in Kubernetes."""
        print(f"[debug] waiting for service {service} in ns={self.namespace}")
        t0 = time.time()
        while time.time() - t0 < timeout:
            result = subprocess.run(
                f"kubectl -n {self.namespace} get svc {service}",
                shell=True,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print(f"[debug] found service {service}")
                return
            time.sleep(3)
        raise RuntimeError(f"Service {service} not found within {timeout}s")

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

            command = f"kubectl port-forward svc/jaeger-out {self.port}:16686 -n observe"
            self.port_forward_process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.environ["JAEGER_PORT"] = str(self.port)
            time.sleep(3)  # Wait a bit for the port-forward to establish

            if self.port_forward_process.poll() is None:
                print(f"Port forwarding established at {self.port}.")
                os.environ["JAEGER_PORT"] = str(self.port)
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


if __name__ == "__main__":
    jaeger = Jaeger()
