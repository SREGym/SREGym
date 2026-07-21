"""Protected open-loop search traffic and direct application metrics access."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

logger = logging.getLogger("all.infra.workload")


class KubectlPortForward:
    """Maintain a localhost tunnel to a Kubernetes Service."""

    def __init__(self, namespace: str, service: str, remote_port: int):
        self.namespace = namespace
        self.service = service
        self.remote_port = remote_port
        self.local_port: int | None = None
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _healthy(self) -> bool:
        if self.process is None or self.process.poll() is not None or self.local_port is None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", self.local_port)) == 0

    def start(self, timeout: float = 30) -> int:
        with self._lock:
            if self._healthy():
                return int(self.local_port)
            self._stop_locked()
            self.local_port = self._free_port()
            self.process = subprocess.Popen(
                [
                    "kubectl",
                    "port-forward",
                    f"service/{self.service}",
                    f"{self.local_port}:{self.remote_port}",
                    "-n",
                    self.namespace,
                    "--address",
                    "127.0.0.1",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    detail = self.process.stderr.read().strip() if self.process.stderr else ""
                    raise RuntimeError(f"port-forward to service/{self.service} failed: {detail}")
                if self._healthy():
                    return self.local_port
                time.sleep(0.2)
            self._stop_locked()
            raise TimeoutError(f"timed out forwarding service/{self.service}:{self.remote_port}")

    def _stop_locked(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.process is not None and self.process.stderr:
            self.process.stderr.close()
        self.process = None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()


class HotelSearchMetrics:
    """Read normal Prometheus-format metrics exposed by search and rate."""

    def __init__(self, namespace: str):
        self.forwards = {
            "search": KubectlPortForward(namespace, "search", 9092),
            "rate": KubectlPortForward(namespace, "rate", 9091),
        }

    @staticmethod
    def _parse(text: str) -> dict[str, float]:
        metrics: dict[str, float] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                metrics[parts[0]] = float(parts[1])
            except ValueError:
                continue
        return metrics

    def read(self, service: str) -> dict[str, float]:
        forward = self.forwards[service]
        port = forward.start()
        try:
            response = requests.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
            response.raise_for_status()
        except requests.RequestException:
            forward.stop()
            port = forward.start()
            response = requests.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
            response.raise_for_status()
        return self._parse(response.text)

    def snapshot(self) -> dict[str, float]:
        return {**self.read("search"), **self.read("rate")}

    def close(self) -> None:
        for forward in self.forwards.values():
            forward.stop()


@dataclass(frozen=True)
class WorkloadSnapshot:
    submitted: int
    completed: int
    succeeded: int
    actual_rate: float
    success_rate: float
    p95_latency_seconds: float | None


class HotelSearchWorkload:
    """Generate constant-rate requests without coupling arrivals to latency."""

    _PATH = "/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=38.0235&lon=-122.095&locale=en"

    def __init__(
        self,
        namespace: str,
        base_rate: float = 8.0,
        request_timeout: float = 4.0,
        max_workers: int = 192,
    ):
        self.namespace = namespace
        self.base_rate = base_rate
        self.request_timeout = request_timeout
        self.max_workers = max_workers
        self.current_rate = base_rate
        self.frontend = KubectlPortForward(namespace, "frontend", 5000)
        self.metrics = HotelSearchMetrics(namespace)
        self._events: deque[tuple[float, bool, float]] = deque()
        self._submissions: deque[float] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.frontend.start()
        self._stop.clear()
        self.current_rate = self.base_rate
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="hotel-search")
        self._thread = threading.Thread(target=self._schedule, name="hotel-search-scheduler", daemon=True)
        self._thread.start()

    def set_rate(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("workload rate must be positive")
        with self._lock:
            self.current_rate = requests_per_second

    def run_trigger(self, requests_per_second: float, duration_seconds: float) -> None:
        self.set_rate(requests_per_second)
        try:
            time.sleep(duration_seconds)
        finally:
            self.set_rate(self.base_rate)

    def _schedule(self) -> None:
        next_submission = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                rate = self.current_rate
            interval = 1.0 / rate
            now = time.monotonic()
            if now < next_submission:
                self._stop.wait(min(next_submission - now, 0.1))
                continue
            with self._lock:
                self._submissions.append(time.monotonic())
            executor = self._executor
            if executor is not None:
                executor.submit(self._request)
            next_submission += interval
            if next_submission < now - interval:
                next_submission = now + interval

    def _request(self) -> None:
        started = time.monotonic()
        success = False
        try:
            port = self.frontend.start()
            response = requests.get(
                f"http://127.0.0.1:{port}{self._PATH}",
                timeout=self.request_timeout,
            )
            if response.status_code == 200:
                payload = json.loads(response.text)
                success = self._valid_response(payload)
        except (requests.RequestException, ValueError):
            pass
        elapsed = time.monotonic() - started
        with self._lock:
            self._events.append((time.monotonic(), success, elapsed))

    @staticmethod
    def _valid_response(payload) -> bool:
        if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
            return False
        features = payload.get("features")
        return (
            isinstance(features, list)
            and bool(features)
            and all(
                isinstance(feature, dict)
                and feature.get("type") == "Feature"
                and bool(feature.get("id"))
                and isinstance(feature.get("geometry"), dict)
                and feature["geometry"].get("type") == "Point"
                for feature in features
            )
        )

    def snapshot(self, window_seconds: float = 20.0) -> WorkloadSnapshot:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            while self._submissions and self._submissions[0] < now - 900:
                self._submissions.popleft()
            while self._events and self._events[0][0] < now - 900:
                self._events.popleft()
            submitted = sum(timestamp >= cutoff for timestamp in self._submissions)
            events = [event for event in self._events if event[0] >= cutoff]
        completed = len(events)
        succeeded = sum(event[1] for event in events)
        latencies = sorted(event[2] for event in events)
        p95 = None
        if latencies:
            index = min(len(latencies) - 1, int(0.95 * len(latencies)))
            p95 = latencies[index]
        return WorkloadSnapshot(
            submitted=submitted,
            completed=completed,
            succeeded=succeeded,
            actual_rate=submitted / window_seconds,
            success_rate=succeeded / completed if completed else 0.0,
            p95_latency_seconds=p95,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self.frontend.stop()
        self.metrics.close()
