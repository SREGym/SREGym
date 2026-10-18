import json
import subprocess
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

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
# Prometheus evaluates rules once per minute and the error-rate rules look back
# 2 minutes, so a fault's own alert can keep firing for minutes after a correct
# fix (SREGym#1116). Keep polling that long before calling the alerts
# persistent, as the Stratus weak AlertOracle does.
_RESOLVE_GRACE_SECONDS = 300
# Room for the last poll and Prometheus query on top of the waiting windows.
_EVALUATION_MARGIN_SECONDS = 60


class AlertOracle(Oracle):
    """Mitigation oracle that passes when no Prometheus alerts are firing.

    Queries the Prometheus alerts API for firing alerts scoped to the
    problem's namespace.  Because alerts can be flaky, the oracle waits
    for a sustained silence window before declaring success.  An alert
    that is still firing fails the check only after
    ``resolve_grace_seconds`` of polling, because a fault's own alert can
    keep firing for minutes after a correct fix.
    """

    importance = 1.0

    # An alert still firing here is treated as the agent's failure, not as
    # ambiguous, because everything this oracle does exists to make that call
    # decisive: ``capture_baseline`` removes alerts that were already firing
    # before injection (SREGym#745), ``exclude_alerts`` removes known-chronic
    # ones, and the verdict requires a *sustained* silence window rather than an
    # instant. What survives all three is a new, persistent alert in the
    # problem's namespace. The residual risk -- an alert triggered by unrelated
    # cluster degradation mid-run -- is what the environment-health precondition
    # is for, not something to hedge by calling every failure ambiguous.
    FAILURE_CLASSES = {
        "alerts_still_firing": FailureClass.AGENT_ERROR,
    }

    def __init__(
        self,
        problem,
        sustained_silence_seconds=_SUSTAINED_SILENCE_SECONDS,
        poll_interval_seconds=_POLL_INTERVAL_SECONDS,
        buffer_seconds=_BUFFER_SECONDS,
        exclude_alerts=None,
        resolve_grace_seconds=_RESOLVE_GRACE_SECONDS,
    ):
        super().__init__(problem)
        self.sustained_silence_seconds = sustained_silence_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.buffer_seconds = buffer_seconds
        self.exclude_alerts = set(exclude_alerts or [])
        self.resolve_grace_seconds = resolve_grace_seconds
        # The longest evaluation: the buffer, alerts that keep firing for the
        # whole grace window, then a full silence window. The conductor's
        # default wait for a submitted evaluation is shorter than that.
        self.evaluation_timeout_seconds = (
            buffer_seconds + resolve_grace_seconds + sustained_silence_seconds + _EVALUATION_MARGIN_SECONDS
        )
        # Alert *instances* (full label sets, not just alertname) already firing
        # before the fault was injected (environmental noise unrelated to the
        # agent). Populated by ``capture_baseline`` and ignored during
        # evaluation. Keying on the full label set (not just alertname) means a
        # pre-existing alert on one pod/service does not mask a newly firing
        # alert with the same name on a *different* pod/service. ``None`` means
        # no baseline was captured, in which case no baseline filtering is
        # applied. See SREGym#745.
        self._baseline_instances = None

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
            # are environmental noise, not the agent's responsibility. Matched by
            # full label set (not just alertname), so a pre-existing alert on one
            # pod/service doesn't hide a newly firing alert with the same name on
            # a different pod/service. Alerts the agent newly triggers are absent
            # from the baseline and still caught.
            if self._baseline_instances is not None and self._alert_instance_key(alert) in self._baseline_instances:
                continue
            firing.append(alert)
        return firing

    @staticmethod
    def _alert_instance_key(alert: dict) -> tuple:
        """A hashable key identifying a specific firing alert *instance*.

        Prometheus identifies a firing alert by its full label set, not just
        ``alertname`` — two alerts with the same name but different ``pod`` or
        ``service`` labels are different instances. Using the full label set
        here (rather than just ``alertname``) ensures a pre-existing alert on
        one pod doesn't mask a newly firing alert with the same name elsewhere.
        """
        return tuple(sorted(alert.get("labels", {}).items()))

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
        # ``_baseline_instances`` is still ``None`` here, so this query applies no
        # baseline filtering and returns the true pre-existing firing set.
        pre_existing_alerts = self._query_firing_alerts(namespace)
        self._baseline_instances = {self._alert_instance_key(alert) for alert in pre_existing_alerts}
        if pre_existing_alerts:
            descriptions = ", ".join(self._fmt_alert(alert) for alert in pre_existing_alerts)
            print(f"📋 AlertOracle baseline for {namespace}: ignoring pre-existing alerts [{descriptions}]")
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

        # Poll for sustained silence. A firing alert restarts the silence window,
        # and fails the check once it has outlasted the resolve grace window.
        start = time.monotonic()
        silence_start = None
        last_log_second = -1

        while True:
            try:
                firing = self._query_firing_alerts(namespace)
            except RuntimeError as exc:
                # Prometheus is the instrument we grade *with*, not the thing
                # under test. If it is unreachable we have no evidence either
                # way, and that is the cluster's problem rather than the
                # agent's. Letting this propagate would reach the conductor's
                # exception handler and be recorded as a harness error, which
                # would be wrong twice over: it blames our code, and it hides a
                # real infrastructure failure behind a bare stack trace. This
                # oracle backs 23 problems, so that misattribution would be the
                # single largest source of miscounted failures.
                print(f"❌ Cannot reach Prometheus to check alerts in {namespace}: {exc}")
                return self.fail("prometheus_unreachable", namespace=namespace, error=str(exc))

            now = time.monotonic()
            elapsed_int = int(now - start)
            if firing:
                names = ", ".join(self._fmt_alert(a) for a in firing)
                if now - start >= self.resolve_grace_seconds:
                    print(f"❌ Firing alerts in {namespace} after {self.resolve_grace_seconds}s: {names}")
                    return self.fail(
                        "alerts_still_firing",
                        namespace=namespace,
                        alerts=sorted({a.get("labels", {}).get("alertname", "") for a in firing}),
                    )
                silence_start = None
                if elapsed_int >= last_log_second + 30:
                    print(f"⏳ Firing alerts in {namespace}: {names} (waiting for them to resolve)")
                    last_log_second = elapsed_int
            else:
                if silence_start is None:
                    silence_start = now
                silent = now - silence_start
                if silent >= self.sustained_silence_seconds:
                    break
                if elapsed_int >= last_log_second + 30:
                    print(f"🔇 No alerts firing — silence for {int(silent)}/{self.sustained_silence_seconds}s")
                    last_log_second = elapsed_int

            time.sleep(self.poll_interval_seconds)

        print(f"✅ No alerts firing in {namespace} for {self.sustained_silence_seconds}s")
        return {"success": True}
