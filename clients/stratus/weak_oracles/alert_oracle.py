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
# Prometheus evaluates rules once per minute (observer values.yaml) and the
# locust error-rate rules compare a 2-minute window, so an alert can stay
# "firing" for up to ~3.5 minutes after the fault is fixed (2 min window +
# 1 min evaluation + scrape). Keep polling for that long before declaring the
# alerts persistent; a measured fix cleared the alerts 138-183 s after it was
# applied.
_RESOLVE_GRACE_SECONDS = 300
_SUBMISSION_RESERVE_SECONDS = 30


def get_validation_deadline() -> float | None:
    """Convert the runner's remaining budget to this process's monotonic clock."""
    start = time.monotonic()
    try:
        api_hostname = os.getenv("API_HOSTNAME", "localhost")
        api_port = os.getenv("API_PORT", "8000")
        response = requests.get(f"http://{api_hostname}:{api_port}/status", timeout=5)
        response.raise_for_status()
        remaining = response.json().get("agent_remaining_seconds")
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            return start + max(0.0, remaining - _SUBMISSION_RESERVE_SECONDS)
    except (requests.RequestException, ValueError, TypeError):
        logger.warning("Could not read the benchmark's remaining agent budget")
        # An unavailable budget must not start a long optional check. Submit the
        # current state to the authoritative evaluator instead.
        return start
    return None


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
        resolve_grace_seconds: int = _RESOLVE_GRACE_SECONDS,
        *,
        deadline: float | None = None,
    ):
        self.namespace = namespace
        self.sustained_silence_seconds = sustained_silence_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.buffer_seconds = buffer_seconds
        self.resolve_grace_seconds = resolve_grace_seconds
        self.deadline = deadline

    def _wait(self, seconds: float) -> bool:
        """Wait only within the validation budget; leave the submission reserve intact."""
        if self.deadline is not None:
            seconds = min(seconds, max(0.0, self.deadline - time.monotonic()))
        time.sleep(seconds)
        return self.deadline is None or time.monotonic() < self.deadline

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
            timeout = 15
            if self.deadline is not None:
                timeout = min(timeout, self.deadline - time.monotonic())
                if timeout <= 0:
                    return None
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
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
        budget_exhausted = OracleResult(success=None, issues=["Validation budget exhausted; submit for final grading"])
        if not self._wait(self.buffer_seconds):
            return budget_exhausted

        start = time.monotonic()
        silence_start = None
        last_names = ""
        while True:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                return budget_exhausted
            status = _get_benchmark_status()
            if status in ("tearing_down", "done"):
                logger.info(f"[AlertOracle] Benchmark is '{status}', stopping alert polling.")
                return OracleResult(success=None, issues=[f"Benchmark is {status}; alerts were not fully checked"])

            firing = self._query_firing_alerts()
            if firing is None:
                return OracleResult(success=None, issues=["Could not query Prometheus alerts"])
            now = time.monotonic()
            if self.deadline is not None and now >= self.deadline:
                return budget_exhausted
            if firing:
                last_names = ", ".join(a.get("labels", {}).get("alertname", "?") for a in firing)
                # Alerts need up to a scrape + evaluation interval to resolve after
                # a fix. Give them that time instead of failing on the first poll.
                if now - start >= self.resolve_grace_seconds:
                    logger.info(
                        f"[AlertOracle] FAIL — alerts still firing in namespace '{self.namespace}' after "
                        f"{self.resolve_grace_seconds}s: {last_names}"
                    )
                    return OracleResult(success=False, issues=[f"Firing alerts: {last_names}"])
                logger.info(f"Firing alerts in {self.namespace}: {last_names} (waiting for them to resolve)")
                silence_start = None
            else:
                if silence_start is None:
                    silence_start = now
                remaining = self.sustained_silence_seconds - (now - silence_start)
                if remaining <= 0:
                    break

            if not self._wait(self.poll_interval_seconds):
                return budget_exhausted

        logger.info(
            f"[AlertOracle] PASS — no firing alerts detected in namespace '{self.namespace}' for {self.sustained_silence_seconds}s"
        )
        return OracleResult(success=True, issues=[])
