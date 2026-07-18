import json
import subprocess
import time

from sregym.conductor.oracles.base import Oracle

# Prometheus endpoint used from *inside* the prometheus-server pod via
# ``kubectl exec``.  We use localhost so the request doesn't depend on
# cluster DNS (which may be broken by fault-injection scenarios such as
# stale_coredns_config).
_PROMETHEUS_URL = "http://localhost:9090"

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
        exclude_alerts=None,
    ):
        super().__init__(problem)
        self.sustained_silence_seconds = sustained_silence_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.buffer_seconds = buffer_seconds
        self.exclude_alerts = set(exclude_alerts or [])
        # Alert names already firing before the fault was injected (environmental
        # noise unrelated to the agent). Populated by ``capture_baseline`` and
        # ignored during evaluation. ``None`` means no baseline was captured, in
        # which case no baseline filtering is applied. See SREGym#745.
        self._baseline_alertnames = None

    # ------------------------------------------------------------------
    # Prometheus query helpers
    # ------------------------------------------------------------------

    def _query_firing_alerts(self, namespace: str) -> list[dict]:
        """Return currently firing alerts for *namespace* via the Prometheus API.

        Uses ``kubectl exec`` into the prometheus-server pod with localhost
        so we don't depend on cluster DNS (which may be broken by fault
        injection, e.g. stale_coredns_config).
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
            raise RuntimeError("Failed to query Prometheus alerts") from exc

        firing = []
        for alert in payload.get("data", {}).get("alerts", []):
            if alert.get("state") != "firing":
                continue
            labels = alert.get("labels", {})
            if labels.get("namespace") != namespace:
                continue
            alertname = labels.get("alertname")
            if alertname in self.exclude_alerts:
                continue
            # Skip alerts that were already firing before fault injection. They
            # are environmental noise, not the agent's responsibility. Alerts the
            # agent newly triggers are absent from the baseline and still caught.
            if self._baseline_alertnames is not None and alertname in self._baseline_alertnames:
                continue
            firing.append(alert)
        return firing

    def capture_baseline(self) -> None:
        """Snapshot alerts already firing in the namespace before fault injection.

        Called by the conductor right before ``inject_fault`` (while the app is
        deployed and healthy but the fault is not yet active). The captured alert
        names are environmental/chronic noise unrelated to the injected fault —
        for example ``ContainerCPUThrottling`` from the astronomy-shop Grafana
        sidecar (see SREGym#745) — and are ignored when grading mitigation so the
        oracle measures the agent's work, not pre-existing noise. Alerts the agent
        newly triggers are not in this baseline and are still caught.
        """
        namespace = self.problem.namespace
        # ``_baseline_alertnames`` is still ``None`` here, so this query applies no
        # baseline filtering and returns the true pre-existing firing set.
        pre_existing = {alert.get("labels", {}).get("alertname") for alert in self._query_firing_alerts(namespace)}
        pre_existing.discard(None)
        self._baseline_alertnames = pre_existing
        if pre_existing:
            print(
                f"📋 AlertOracle baseline for {namespace}: ignoring pre-existing alerts "
                f"[{', '.join(sorted(pre_existing))}]"
            )
        else:
            print(f"📋 AlertOracle baseline for {namespace}: no pre-existing alerts")

    def _query_max_alert_for_duration(self) -> float:
        """Return the longest *for* duration (seconds) across all Prometheus alert rules.

        Queries ``/api/v1/rules`` via ``kubectl exec``.  Falls back to 0 if the
        query fails or no rules are found.
        """
        url = f"{_PROMETHEUS_URL}/api/v1/rules"
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
            print(f"⚠️  Failed to query Prometheus rules: {exc}")
            return 0.0

        max_duration = 0.0
        for group in payload.get("data", {}).get("groups", []):
            for rule in group.get("rules", []):
                if rule.get("type") != "alerting":
                    continue
                duration = rule.get("duration", 0) or 0
                if duration > max_duration:
                    max_duration = duration
        return max_duration

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
