import os
import socket
import subprocess
import time
from pathlib import Path


class JaegerTiDB:
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
                shell=True, capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"[debug] found service {service}")
                return
            time.sleep(3)
        raise RuntimeError(f"Service {service} not found within {timeout}s")

    def start_port_forward(self, service: str = "jaeger-out", timeout: int = 30):
        """Start port-forwarding Jaeger UI to localhost:16686 and keep it alive."""
        print(f"[debug] Entering port forward")

        self.wait_for_service(service)

        if self.port_forward_process and self.port_forward_process.poll() is None:
            print("Port-forwarding already active.")
            return

        if self.is_port_in_use(self.port):
            raise RuntimeError(f"Port {self.port} is already in use on localhost.")

        command = [
            "kubectl", "-n", "observe",
            "port-forward", "svc/jaeger-out", "16686:16686"
        ]

        self.port_forward_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[debug] starting Jaeger port-forward: {' '.join(command)}")


        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.is_port_in_use(self.port):
                print(f"Jaeger UI available at http://localhost:{self.port}")
                return
            if self.port_forward_process.poll() is not None:
                raise RuntimeError(
                    f"kubectl port-forward exited early (code {self.port_forward_process.returncode})"
                )
            time.sleep(1)

        raise RuntimeError("Port-forward did not establish within timeout")

    def stop_port_forward(self):
        """Stop the kubectl port-forward process."""
        if self.port_forward_process:
            self.port_forward_process.terminate()
            try:
                self.port_forward_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("Force killing Jaeger port-forward...")
                self.port_forward_process.kill()
            self.port_forward_process = None
            print("Jaeger port-forward stopped.")

    def main(self):
        self.deploy()
        self.start_port_forward()
        

        print("Jaeger deployment and port-forward complete.")


if __name__ == "__main__":
    jaeger = JaegerTiDB()
    jaeger.main()
