import json
import logging
import subprocess
import time

from clients.stratus.weak_oracles.base_oracle import BaseOracle, OracleResult

logger = logging.getLogger("all.stratus.alert_oracle")

_PROMETHEUS_URL = "http://prometheus-server.observe.svc:80"
_SUSTAINED_SILENCE_SECONDS = 120
_POLL_INTERVAL_SECONDS = 10
_BUFFER_SECONDS = 30


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

    def _query_firing_alerts(self) -> list[dict]:
        url = f"{_PROMETHEUS_URL}/api/v1/alerts"
        cmd = ["kubectl", "exec", "-n", "observe", "deploy/prometheus-server", "--", "wget", "-qO-", url]
        try:
            raw = subprocess.check_output(cmd, text=True, timeout=15)
            payload = json.loads(raw)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
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
            elapsed = time.monotonic() - start
            if elapsed >= self.sustained_silence_seconds:
                break

            firing = self._query_firing_alerts()
            if firing:
                names = ", ".join(a.get("labels", {}).get("alertname", "?") for a in firing)
                logger.info(f"Firing alerts in {self.namespace}: {names}")
                return OracleResult(success=False, issues=[f"Firing alerts: {names}"])

            time.sleep(self.poll_interval_seconds)

        return OracleResult(success=True, issues=[])
