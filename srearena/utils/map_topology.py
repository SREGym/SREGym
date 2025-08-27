import argparse
import os
import select
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
import requests

import pandas as pd

class TopologyMapper:
    def __init__(self, namespace: str):
        self.port_forward_process = None
        self.namespace = namespace
        self.stop_event = threading.Event()
        self.output_threads = []

        if self.namespace == "astronomy-shop":
            # No NodePort in astronomy shop
            self.base_url = "http://localhost:16686/jaeger/ui"
            self.start_port_forward()
        else:
            # Other namespaces may expose a NodePort
            node_port = self.get_nodeport("jaeger", namespace)
            if node_port:
                self.base_url = f"http://localhost:{node_port}"
            else:
                self.base_url = "http://localhost:16686"
                self.start_port_forward()

    def get_nodeport(self, service_name, namespace):
        """Fetch the NodePort for the given service."""
        try:
            result = subprocess.check_output(
                [
                    "kubectl",
                    "get",
                    "service",
                    service_name,
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={.spec.ports[0].nodePort}",
                ],
                text=True,
            )
            nodeport = result.strip()
            print(f"NodePort for service {service_name}: {nodeport}")
            return nodeport
        except subprocess.CalledProcessError as e:
            print(f"Error getting NodePort: {e.output}")
            return None

    def print_output(self, stream):
        """Thread function to print output from a subprocess stream non-blockingly."""
        while not self.stop_event.is_set():
            # Check if there is content to read
            ready, _, _ = select.select([stream], [], [], 0.1)  # 0.1-second timeout
            if ready:
                try:
                    line = stream.readline()
                    if line:
                        print(line, end="")
                    else:
                        break  # Exit if no more data and process ended
                except ValueError as e:
                    print("Stream closed:", e)
                    break
            if self.port_forward_process.poll() is not None:
                break

    def is_port_in_use(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def get_jaeger_pod_name(self):
        try:
            result = subprocess.check_output(
                [
                    "kubectl",
                    "get",
                    "pods",
                    "-n",
                    self.namespace,
                    "-l",
                    "app.kubernetes.io/name=jaeger",
                    "-o",
                    "jsonpath={.items[0].metadata.name}",
                ],
                text=True,
            )
            return result.strip()
        except subprocess.CalledProcessError as e:
            print("Error getting Jaeger pod name:", e)
            raise

    def start_port_forward(self):
        """Starts kubectl port-forward command to access Jaeger service or pod."""
        for attempt in range(3):
            if self.is_port_in_use(16686):
                print(f"Port 16686 is already in use. Attempt {attempt + 1} of 3. Retrying in 3 seconds...")
                time.sleep(3)
                continue

            # Use pod port-forwarding for astronomy-shop only
            if self.namespace == "astronomy-shop":
                pod_name = self.get_jaeger_pod_name()
                command = f"kubectl port-forward pod/{pod_name} 16686:16686 -n {self.namespace}"
            else:
                command = f"kubectl port-forward svc/jaeger 16686:16686 -n {self.namespace}"

            print("Starting port-forward with command:", command)
            self.port_forward_process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            thread_out = threading.Thread(target=self.print_output, args=(self.port_forward_process.stdout,))
            thread_err = threading.Thread(target=self.print_output, args=(self.port_forward_process.stderr,))
            thread_out.start()
            thread_err.start()

            time.sleep(3)  # Let port-forward initialize

            if self.port_forward_process.poll() is None:
                print("Port forwarding established successfully.")
                break
            else:
                print("Port forwarding failed. Retrying...")
        else:
            print("Failed to establish port forwarding after 3 attempts.")

        # TODO: modify this command for other microservices
        # command = "kubectl port-forward svc/jaeger 16686:16686 -n hotel-reservation"
        # self.port_forward_process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # thread_out = threading.Thread(target=self.print_output, args=(self.port_forward_process.stdout,))
        # thread_err = threading.Thread(target=self.print_output, args=(self.port_forward_process.stderr,))
        # thread_out.start()
        # thread_err.start()
        # # self.output_threads.extend([thread_out, thread_err])
        # time.sleep(3)  # Wait a bit for the port-forward to establish

    def stop_port_forward(self):
        if self.port_forward_process:
            self.stop_event.set()  # Signal threads to exit
            try:
                self.port_forward_process.terminate()
                self.port_forward_process.wait(timeout=5)
            except Exception as e:
                print("Error terminating port-forward process:", e)

            try:
                if self.port_forward_process.stdout:
                    self.port_forward_process.stdout.close()
                if self.port_forward_process.stderr:
                    self.port_forward_process.stderr.close()
            except Exception as e:
                print("Error closing process streams:", e)
            self.port_forward_process = None

    def cleanup(self):
        self.stop_port_forward()
        for thread in self.output_threads:
            thread.join(timeout=5)
            if thread.is_alive():
                print(f"Thread {thread.name} could not be joined and may need to be stopped forcefully.")
        self.output_threads.clear()
        print("Cleanup completed.")

    def extract_topology_map(self) -> pd.DataFrame:
        url = f"{self.base_url}/api/dependencies"
        headers = {"Accept": "application/json"} if self.namespace == "astronomy-shop" else {}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            topology_data = response.json().get("data", [])
        except Exception as e:
            print(f"Failed to get services: {e}")
            return []

        df = pd.DataFrame(topology_data)[["parent", "child", "callCount"]]
        df = df.rename(columns={"parent": "source", "child": "target", "callCount": "weight"})
        df["weight"] = 1

        output_dir = "srearena/utils/topology_graph"
        os.makedirs(output_dir, exist_ok=True)

        output_path = os.path.join(output_dir, f"{self.namespace}.csv")
        df.to_csv(output_path, index=False)

        self.cleanup()
        print("Cleanup completed.")
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract Jaeger topology map")
    parser.add_argument(
        "--namespace", 
        required=True, 
        help="Namespace to extract topology for"
    )

    parser.add_argument(
        "--minutes", 
        type=int, 
        default=60, 
        help="Time window in minutes (default: 60)"
    )
    args = parser.parse_args()

    mapper = TopologyMapper(namespace=args.namespace)
    end_time = datetime.now()
    start_time = end_time - timedelta(minutes=args.minutes)  # Example time window
    mapper.extract_topology_map()