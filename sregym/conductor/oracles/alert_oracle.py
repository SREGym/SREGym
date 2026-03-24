import json
import subprocess
import time

from sregym.conductor.oracles.base import Oracle

# Prometheus service endpoint inside the cluster.
_PROMETHEUS_URL = "http://prometheus-server.observe.svc:80"

# How long to monitor for sustained alert silence.
_SUSTAINED_SILENCE_SECONDS = 120
_POLL_INTERVAL_SECONDS = 10
# Grace period before starting to check (let alerts resolve).
_BUFFER_SECONDS = 30


class AlertOracle(Oracle):
    """Mitigation oracle that passes when no Prometheus alerts are firing.

    Queries the Prometheus alerts API for firing alerts scoped to the
    problem's namespace.  Because alerts can be flaky, the oracle waits
    for a sustained silence window before declaring success.
    """

    importance = 1.0

    def __init__(
        self,
        problem,
        sustained_silence_seconds=_SUSTAINED_SILENCE_SECONDS,
        poll_interval_seconds=_POLL_INTERVAL_SECONDS,
        buffer_seconds=_BUFFER_SECONDS,
    ):
        super().__init__(problem)
        self.sustained_silence_seconds = sustained_silence_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.buffer_seconds = buffer_seconds

    # ------------------------------------------------------------------
    # Prometheus query helpers
    # ------------------------------------------------------------------

    def _query_firing_alerts(self, namespace: str) -> list[dict]:
        """Return currently firing alerts for *namespace* via the Prometheus API.

        Uses ``kubectl exec`` into the prometheus-server pod so we don't
        need port-forwarding or external access.
        """
        url = f"{_PROMETHEUS_URL}/api/v1/alerts"
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
            url,
        ]
        try:
            raw = subprocess.check_output(cmd, text=True, timeout=15)
            payload = json.loads(raw)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            print(f"⚠️  Failed to query Prometheus alerts: {exc}")
            return []

        firing = []
        for alert in payload.get("data", {}).get("alerts", []):
            if alert.get("state") != "firing":
                continue
            labels = alert.get("labels", {})
            if labels.get("namespace") == namespace:
                firing.append(alert)
        return firing

    @staticmethod
    def _fmt_alert(alert: dict) -> str:
        labels = alert.get("labels", {})
        name = labels.get("alertname", "?")
        svc = labels.get("service_name") or labels.get("pod") or ""
        severity = labels.get("severity", "")
        return f"{name} ({svc}) [{severity}]"

    # ------------------------------------------------------------------
    # Oracle interface
    # ------------------------------------------------------------------

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        print("== Alert Oracle Evaluation ==")

        namespace = self.problem.namespace

        # Buffer: give alerts time to resolve after mitigation.
        print(f"⏳ Waiting {self.buffer_seconds}s buffer before checking alerts…")
        time.sleep(self.buffer_seconds)

        # Poll for sustained silence. Any firing alert is an immediate failure.
        start = time.monotonic()
        last_log_second = -1

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= self.sustained_silence_seconds:
                break

            firing = self._query_firing_alerts(namespace)

            if firing:
                names = ", ".join(self._fmt_alert(a) for a in firing)
                print(f"❌ Firing alerts in {namespace}: {names}")
                return {"success": False}

            elapsed_int = int(elapsed)
            if elapsed_int >= last_log_second + 30:
                print(f"🔇 No alerts firing — silence for {elapsed_int}/{self.sustained_silence_seconds}s")
                last_log_second = elapsed_int

            time.sleep(self.poll_interval_seconds)

        print(f"✅ No alerts firing in {namespace} for {self.sustained_silence_seconds}s")
        return {"success": True}
