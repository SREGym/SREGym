import json
import logging
import os
import subprocess
import time

import requests

from clients.stratus.weak_oracles.base_oracle import BaseOracle, OracleResult

logger = logging.getLogger("all.stratus.alert_oracle")

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
        """Return firing alerts, or None when Prometheus cannot be checked."""
        # The filtered Kubernetes proxy blocks Service proxy subresources. Exec
        # into Prometheus instead, as the conductor's AlertOracle already does.
        cmd = [
            "kubectl",
            "exec",
            "-n",
            "observe",
            "deploy/prometheus-server",
            "-c",
            "prometheus-server",
            "--",
            "wget",
            "-qO-",
            "http://localhost:9090/api/v1/alerts",
        ]
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=15)
            if result.returncode != 0:
                logger.warning(
                    "Failed to query Prometheus alerts: exit %s; stderr: %r", result.returncode, result.stderr
                )
                return None
            payload = json.loads(result.stdout)
            alerts = payload["data"]["alerts"]
            if payload.get("status") != "success" or not isinstance(alerts, list):
                raise ValueError("Unexpected Prometheus alerts response")
            firing = []
            for alert in alerts:
                labels = alert["labels"]
                if not isinstance(labels, dict):
                    raise ValueError("Unexpected Prometheus alert labels")
                if alert["state"] == "firing" and labels.get("namespace") == self.namespace:
                    firing.append(alert)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as exc:
            logger.warning("Failed to query Prometheus alerts: %s", exc)
            return None

        return firing

    def validate(self) -> OracleResult:
        logger.info(f"Waiting {self.buffer_seconds}s before checking alerts...")
        time.sleep(self.buffer_seconds)

        start = time.monotonic()
        while True:
            status = _get_benchmark_status()
            if status in ("tearing_down", "done"):
                logger.info(f"[AlertOracle] Benchmark is '{status}', stopping alert polling.")
                return OracleResult(success=None, issues=[f"Benchmark is {status}; alerts were not fully checked"])

            firing = self._query_firing_alerts()
            if firing is None:
                return OracleResult(success=None, issues=["Could not query Prometheus alerts"])
            if firing:
                names = ", ".join(a.get("labels", {}).get("alertname", "?") for a in firing)
                logger.info(f"Firing alerts in {self.namespace}: {names}")
                logger.info(f"[AlertOracle] FAIL — firing alerts detected in namespace '{self.namespace}': {names}")
                return OracleResult(success=False, issues=[f"Firing alerts: {names}"])

            remaining = self.sustained_silence_seconds - (time.monotonic() - start)
            if remaining <= 0:
                break
            time.sleep(min(self.poll_interval_seconds, remaining))

        logger.info(
            f"[AlertOracle] PASS — no firing alerts detected in namespace '{self.namespace}' for {self.sustained_silence_seconds}s"
        )
        return OracleResult(success=True, issues=[])
