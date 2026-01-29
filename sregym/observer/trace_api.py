import logging
import os
import select
import socket
import subprocess
import threading
import time
from datetime import datetime
from typing import List, Optional

import pandas as pd
import requests


class TraceAPI:
    """
    Jaeger HTTP API helper.

    - For most apps:
        * Prefer NodePort on svc/jaeger
        * Otherwise port-forward svc/jaeger :16686
        * Base URL: http://127.0.0.1:<port>

    - For Astronomy Shop:
        * Jaeger is exposed THROUGH the frontend proxy on 8080, under /jaeger
        * Prefer NodePort on svc/frontend-proxy
        * Otherwise port-forward svc/frontend-proxy :8080
        * Base URL: http://127.0.0.1:<port>/jaeger
    """

    _instance_lock: threading.Lock

    def __init__(self, namespace: str, prefer_nodeport: bool = True, pf_ready_sleep: float = 2.0):
        self.namespace = namespace
        self.prefer_nodeport = prefer_nodeport
        self.pf_ready_sleep = pf_ready_sleep

        self.port_forward_process: Optional[subprocess.Popen] = None
        self.local_port: Optional[int] = None
        self.stop_event = threading.Event()
        self.output_threads: List[threading.Thread] = []
        self._instance_lock = threading.Lock()

        # Decide service/port/prefix based on namespace
        self._is_astronomy = self.namespace == "astronomy-shop"
        self._svc_name = "frontend-proxy" if self._is_astronomy else "jaeger"
        self._remote_port = "8080" if self._is_astronomy else "16686"
        self._url_prefix = "/jaeger/ui" if self._is_astronomy else ""

        self.logger = logging.getLogger("all.infra.trace_api")
        self.logger.propagate = True
        self.logger.setLevel(logging.DEBUG)

        # Choose access path: NodePort (if available) else port-forward
        node_port = None
        if self.prefer_nodeport:
            node_port = self.get_nodeport(self._svc_name, self.namespace)

        if node_port:
            # Use NodePort directly
            self.base_url = f"http://localhost:{node_port}{self._url_prefix}"
            self.using_port_forward = False
            self._export_env(port=node_port)  # <<< ensure env set for NodePort (incl. astronomy-shop)
        else:
            # Fall back to port-forward on a free local port
            self.using_port_forward = True
            self.start_port_forward()  # sets base_url and env

    # ------------------------
    # Cluster discovery helpers
    # ------------------------

    def get_nodeport(self, service_name: str, namespace: str) -> Optional[str]:
        """Return NodePort string if present; otherwise None."""
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
            ).strip()
            if result:
                self.logger.info(f"NodePort for service {service_name}: {result}")
                return result
            return None
        except subprocess.CalledProcessError as e:
            msg = (e.output or "").strip()
            if msg:
                self.logger.error(f"Error getting NodePort: {msg}")
            return None

    def get_jaeger_pod_name(self) -> str:
        """Resolve the Jaeger pod name (if you ever need pod forwarding)."""
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
            name = result.strip()
            if not name:
                raise RuntimeError("No Jaeger pods found")
            return name
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Error getting Jaeger pod name: {e.output}") from e

    # ------------------------
    # Port-forward management
    # ------------------------

    @staticmethod
    def _pick_free_port() -> int:
        """Pick a free local TCP port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    def _build_pf_cmd(self, local_port: int) -> list:
        """
        Build a kubectl port-forward command that binds only to 127.0.0.1.
        """
        target = f"svc/{self._svc_name}"
        return [
            "kubectl",
            "-n",
            self.namespace,
            "port-forward",
            target,
            f"{local_port}:{self._remote_port}",
            "--address",
            "127.0.0.1",
        ]

    def _print_output(self, stream):
        """Non-blocking reader for subprocess stdout/stderr."""
        while not self.stop_event.is_set():
            if self.port_forward_process and self.port_forward_process.poll() is not None:
                break
            try:
                ready, _, _ = select.select([stream], [], [], 0.1)
            except (ValueError, OSError):
                break
            if ready:
                line = stream.readline()
                if line:
                    self.logger.info(line.rstrip())
                else:
                    break

    def start_port_forward(self):
        """Start kubectl port-forward exactly once; idempotent."""
        with self._instance_lock:
            if self.port_forward_process and self.port_forward_process.poll() is None:
                return

            self.local_port = self._pick_free_port()
            cmd = self._build_pf_cmd(self.local_port)

            msg = "Starting port-forward with command:" + " ".join(cmd)
            self.logger.info(msg)
            self.port_forward_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if self.port_forward_process.stdout:
                t_out = threading.Thread(
                    target=self._print_output, args=(self.port_forward_process.stdout,), daemon=True
                )
                t_out.start()
                self.output_threads.append(t_out)
            if self.port_forward_process.stderr:
                t_err = threading.Thread(
                    target=self._print_output, args=(self.port_forward_process.stderr,), daemon=True
                )
                t_err.start()
                self.output_threads.append(t_err)

        # Let kubectl set up the tunnel
        time.sleep(self.pf_ready_sleep)

        if self.port_forward_process and self.port_forward_process.poll() is None:
            self.logger.info(f"Port forwarding established successfully on")
            self.base_url = f"http://127.0.0.1:{self.local_port}{self._url_prefix}"
            self._export_env(port=self.local_port)  # <<< ensure env set for PF case (incl. astronomy-shop)
        else:
            raise RuntimeError("Port forwarding failed to start")

    def stop_port_forward(self):
        """Terminate kubectl and close streams."""
        with self._instance_lock:
            if not self.port_forward_process:
                return

            self.stop_event.set()
            try:
                self.port_forward_process.terminate()
                self.port_forward_process.wait(timeout=5)
            except Exception as e:
                self.logger.error(f"Error terminating port-forward process:", e)
                try:
                    self.port_forward_process.kill()
                except Exception:
                    pass

            try:
                if self.port_forward_process.stdout:
                    self.port_forward_process.stdout.close()
                if self.port_forward_process.stderr:
                    self.port_forward_process.stderr.close()
            except Exception as e:
                self.logger.error(f"Error closing process streams:", e)

            self.port_forward_process = None
            self.local_port = None

        for t in self.output_threads:
            t.join(timeout=2)
        self.output_threads.clear()
        self.logger.info("Port-forward stopped.")

    def cleanup(self):
        """Public cleanup (safe to call multiple times)."""
        if self.using_port_forward:
            self.stop_port_forward()
        self.logger.info("Cleanup completed.")

    # ------------------------
    # Environment export
    # ------------------------

    def _export_env(self, port: int):
        """
        Standardize env for downstream tools:
          - JAEGER_PORT: the local port to reach Jaeger (NodePort or PF)
          - JAEGER_BASE_URL: full base URL (includes prefix for astronomy-shop)
        """
        os.environ["JAEGER_PORT"] = str(port)
        os.environ["JAEGER_BASE_URL"] = self.base_url

    # ------------------------
    # Jaeger API wrappers
    # ------------------------

    @staticmethod
    def _api_headers():
        # Some proxies are picky; be explicit about JSON.
        return {"Accept": "application/json"}

    def get_services(self) -> List[str]:
        """Fetch list of service names known to Jaeger."""
        url = f"{self.base_url}/api/services"
        try:
            resp = requests.get(url, headers=self._api_headers(), timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []) or []
        except Exception as e:
            self.logger.error(f"Failed to get services: {e}")
            return []

    def get_traces(
        self, service_name: str, start_time: datetime, end_time: datetime, limit: Optional[int] = None
    ) -> list:
        """
        Fetch traces for a service between start_time and end_time.
        Jaeger HTTP API supports lookback + optional limit.
        """
        lookback_sec = int((datetime.now() - start_time).total_seconds())
        url = f"{self.base_url}/api/traces?service={service_name}&lookback={lookback_sec}s"
        if limit is not None:
            url += f"&limit={limit}"

        try:
            resp = requests.get(url, headers=self._api_headers(), timeout=15)
            resp.raise_for_status()
            return resp.json().get("data", []) or []
        except Exception as e:
            self.logger.error(f"Failed to get traces for {service_name}: {e}")
            return []

    def extract_traces(self, start_time: datetime, end_time: datetime, limit: Optional[int] = None) -> list:
        """
        Extract traces across all services (except utility ones) in the time range.
        Automatically calls cleanup() when done.
        """
        try:
            services = self.get_services()
            self.logger.info(f"services: {services}")
            all_traces = []
            if not services:
                self.logger.error("No services found.")
                return all_traces

            for svc in services:
                if svc == "jaeger-all-in-one":
                    continue
                traces = self.get_traces(svc, start_time, end_time, limit=limit)
                for trace in traces:
                    # Normalize serviceName into spans for easier downstream processing
                    proc_map = trace.get("processes", {})
                    for span in trace.get("spans", []):
                        span["serviceName"] = proc_map.get(span.get("processID"), {}).get("serviceName", "unknown")
                    all_traces.append(trace)
            return all_traces
        finally:
            self.cleanup()

    def process_traces(self, traces: list) -> pd.DataFrame:
        """Flatten raw Jaeger traces into a DataFrame."""
        rows = []
        for trace in traces:
            tid = trace.get("traceID")
            for span in trace.get("spans", []):
                parent_span = "ROOT"
                for ref in span.get("references", []):
                    if ref.get("refType") == "CHILD_OF":
                        parent_span = ref.get("spanID")
                        break

                has_error = False
                response = "Unknown"
                for tag in span.get("tags", []):
                    if tag.get("key") == "error" and bool(tag.get("value")):
                        has_error = True
                    if tag.get("key") in ("http.status_code", "response_class"):
                        response = tag.get("value")

                rows.append(
                    {
                        "trace_id": tid,
                        "span_id": span.get("spanID"),
                        "parent_span": parent_span,
                        "service_name": span.get("serviceName"),
                        "operation_name": span.get("operationName"),
                        "start_time": span.get("startTime"),
                        "duration": span.get("duration"),
                        "has_error": has_error,
                        "response": response,
                    }
                )

        return pd.DataFrame(rows)

    def save_traces(self, df: pd.DataFrame, path: str) -> str:
        os.makedirs(path, exist_ok=True)
        file_path = os.path.join(path, f"traces_{int(time.time())}.csv")
        df.to_csv(file_path, index=False)
        return f"Traces data exported to: {file_path}"
