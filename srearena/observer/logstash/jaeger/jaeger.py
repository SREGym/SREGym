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
        self.port = 16686  # default Jaeger UI port
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

    def start_port_forward(self, retries: int = 3, wait: int = 3):
        """Start port-forwarding Jaeger UI to localhost:16686, retrying if needed."""
        if self.port_forward_process and self.port_forward_process.poll() is None:
            print("Port-forwarding already active.")
            return

        for attempt in range(retries):
            if self.is_port_in_use(self.port):
                print(f"Port {self.port} is in use. Retry {attempt + 1}/{retries} in {wait}s...")
                time.sleep(wait)
                continue

            command = f"kubectl port-forward svc/jaeger-out {self.port}:16686 -n {self.namespace}"
            print(f"Starting Jaeger port-forward with: {command}")
            self.port_forward_process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            time.sleep(2) 
            if self.is_port_in_use(self.port):
                print(f"Jaeger UI available at http://localhost:{self.port}")
                return
            else:
                print("⚠️  Port-forward failed, retrying...")
                if self.port_forward_process:
                    self.port_forward_process.terminate()
                    self.port_forward_process.wait()

        raise RuntimeError("Failed to establish Jaeger port-forward after multiple attempts.")

    def stop_port_forward(self):
        """Stop the kubectl port-forward process."""
        if self.port_forward_process:
            self.port_forward_process.terminate()
            try:
                self.port_forward_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("Force killing Jaeger port-forward...")
                self.port_forward_process.kill()

            if self.port_forward_process.stdout:
                self.port_forward_process.stdout.close()
            if self.port_forward_process.stderr:
                self.port_forward_process.stderr.close()

            print("Jaeger port-forward stopped.")

    def main(self):
        self.deploy()
        self.start_port_forward()


if __name__ == "__main__":
    jaeger = JaegerTiDB()
    jaeger.main()
