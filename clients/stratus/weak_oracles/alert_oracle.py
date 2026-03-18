import json
import logging
import os
import subprocess
import time

import requests

from clients.stratus.weak_oracles.base_oracle import BaseOracle, OracleResult

logger = logging.getLogger("all.stratus.alert_oracle")

_PROMETHEUS_URL = "http://prometheus-server.observe.svc:80"
_SUSTAINED_SILENCE_SECONDS = 120
_POLL_INTERVAL_SECONDS = 10
_BUFFER_SECONDS = 30


def _get_benchmark_status() -> str:
    try:
        api_hostname = os.getenv("API_HOSTNAME", "localhost")
        api_port = os.getenv("API_PORT", "8000")
        response = requests.get(f"http://{api_hostname}:{api_port}/status", timeout=5)
        if response.status_code == 200:
            return response.json().get("stage", "unknown")
    except Exception:
        pass
    return "unknown"


class AlertOracle(BaseOracle):
    """Weak oracle that passes when no Prometheus alerts are firing in the namespace."""

    def __init__(
        self,
        namespace: str,
        sustained_silence_seconds: int = _SUSTAINED_SILENCE_SECONDS,
        poll_interval_seconds: int = _POLL_INTERVAL_SECONDS,
        buffer_seconds: int = _BUFFER_SECONDS,
    ):
        self.namespace = namespace
        self.sustained_silence_seconds = sustained_silence_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.buffer_seconds = buffer_seconds

    def _query_firing_alerts(self) -> list[dict] | None:
        """Returns list of firing alerts, or None if the cluster is being torn down."""
        url = f"{_PROMETHEUS_URL}/api/v1/alerts"
        cmd = ["kubectl", "exec", "-n", "observe", "deploy/prometheus-server", "--", "wget", "-qO-", url]
        try:
            raw = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE, timeout=15)
            payload = json.loads(raw)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            if "NotFound" in stderr:
                logger.info(f"[AlertOracle] Prometheus not found (cluster teardown detected), stopping poll.")
                return None
            logger.warning(f"Failed to query Prometheus alerts: {exc}")
            return []
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            logger.warning(f"Failed to query Prometheus alerts: {exc}")
            return []

        return [
            alert
            for alert in payload.get("data", {}).get("alerts", [])
            if alert.get("state") == "firing" and alert.get("labels", {}).get("namespace") == self.namespace
        ]

    def validate(self) -> OracleResult:
        logger.info(f"Waiting {self.buffer_seconds}s before checking alerts...")
        time.sleep(self.buffer_seconds)

        start = time.monotonic()
        while True:
            status = _get_benchmark_status()
            if status in ("tearing_down", "done"):
                logger.info(f"[AlertOracle] Benchmark is '{status}', stopping alert polling.")
                break

            elapsed = time.monotonic() - start
            if elapsed >= self.sustained_silence_seconds:
                break

            firing = self._query_firing_alerts()
            if firing is None:
                break
            if firing:
                names = ", ".join(a.get("labels", {}).get("alertname", "?") for a in firing)
                logger.info(f"Firing alerts in {self.namespace}: {names}")
                logger.info(f"[AlertOracle] FAIL — firing alerts detected in namespace '{self.namespace}': {names}")
                return OracleResult(success=False, issues=[f"Firing alerts: {names}"])

            time.sleep(self.poll_interval_seconds)

        logger.info(f"[AlertOracle] PASS — no firing alerts detected in namespace '{self.namespace}' for {self.sustained_silence_seconds}s")
        return OracleResult(success=True, issues=[])
