"""Workload manager for the load generators shipped with the Incident Arena apps.

Each Incident Arena chart deploys its own seeded, open-loop load generator
(``loadgen`` Deployment). It records every arrival in a private ledger inside
its pod; this manager starts/stops that Deployment and reads the ledger through
``kubectl exec`` using :mod:`sregym.generators.workload.incident_arena_ledger`.
"""

from __future__ import annotations

import json
import logging
import shlex
import time
from pathlib import Path

from sregym.generators.workload.base import WorkloadEntry, WorkloadManager

logger = logging.getLogger(__name__)

_LEDGER_SCRIPT = Path(__file__).with_name("incident_arena_ledger.py")


class IncidentArenaLoadgen(WorkloadManager):
    """Drive and observe the chart-owned ``loadgen`` Deployment."""

    def __init__(self, namespace: str, kubectl, deployment: str = "loadgen", container: str = "loadgen"):
        super().__init__()
        self.namespace = namespace
        self.kubectl = kubectl
        self.deployment = deployment
        self.container = container

    # ------------------------------------------------------------------ lifecycle
    def start(self, timeout_s: int = 900) -> None:
        """Ensure the load generator runs (it starts with the chart)."""
        self.kubectl.exec_command(
            f"kubectl scale deployment/{self.deployment} -n {self.namespace} --replicas=1",
        )
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.deployment} -n {self.namespace} --timeout={timeout_s}s",
            timeout=timeout_s + 30,
        )

    def stop(self) -> None:
        self.kubectl.exec_command(
            f"kubectl scale deployment/{self.deployment} -n {self.namespace} --replicas=0",
        )

    # ------------------------------------------------------------------ ledger
    def _run_ledger(self, *args: str, timeout: float = 180) -> dict:
        quoted = " ".join(shlex.quote(str(a)) for a in args)
        # Some loadgen images ship only `python`, others `python3`.
        inner = f"if command -v python3 >/dev/null 2>&1; then exec python3 - {quoted}; else exec python - {quoted}; fi"
        command = (
            f"kubectl exec -i -n {self.namespace} deploy/{self.deployment} -c {self.container} -- "
            f"sh -c {shlex.quote(inner)}"
        )
        out = self.kubectl.exec_command_checked(command, input_data=_LEDGER_SCRIPT.read_text(), timeout=timeout)
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        raise RuntimeError(f"load generator ledger returned no JSON: {out[-500:]!r}")

    def latest_sent_s(self) -> float | None:
        """Newest arrival time on the generator's clock (None before any traffic)."""
        return self._run_ledger("latest").get("latest_sent_s")

    def status(self) -> dict:
        """Newest arrival time, the sidecar's episode-end record and its log tail."""
        return self._run_ledger("status")

    def start_episode(self) -> dict:
        """Open the sidecar's episode-start gate (idempotent; the HTTP status it answered)."""
        return self._run_ledger("start")

    def logs(self, tail: int = 40) -> str:
        return self.kubectl.exec_command(
            f"kubectl logs -n {self.namespace} deploy/{self.deployment} -c {self.container} --tail={tail}"
        )

    def restart(self, timeout_s: int = 600) -> None:
        """Replace the load generator pod; its private ledger starts empty."""
        self.kubectl.exec_command_checked(
            f"kubectl rollout restart deployment/{self.deployment} -n {self.namespace}",
        )
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.deployment} -n {self.namespace} --timeout={timeout_s}s",
            timeout=timeout_s + 30,
        )

    def wait_for_traffic(
        self,
        timeout_s: float = 900,
        interval_s: float = 10,
        max_restarts: int = 2,
        restart_delay_s: float = 30,
    ) -> float:
        """Start the episode and block until the ledger has arrivals; return the newest ``sent_s``.

        The sidecar provisions its drivers, then waits for ``POST
        /grader/episode-start`` before sending anything. Incident Arena's
        harness makes that call once the system is healthy; SREGym repeats it
        on every poll until traffic appears. A sidecar that fails before then
        records the error in ``episode_done.json`` and idles. Such an episode
        is restarted up to ``max_restarts`` times before failing with the
        sidecar's own error.
        """
        deadline = time.monotonic() + timeout_s
        restarts = 0
        last_error: Exception | None = None
        status: dict = {}
        start: dict | None = None
        while time.monotonic() < deadline:
            try:
                status = self.status()
                last_error = None
            except Exception as exc:  # pod still starting
                last_error = exc
                time.sleep(interval_s)
                continue
            if status.get("latest_sent_s") is not None:
                return float(status["latest_sent_s"])
            done = status.get("episode_done")
            if done is not None:
                reason = self._describe(status)
                if restarts >= max_restarts:
                    raise RuntimeError(f"load generator episode failed before sending traffic: {reason}")
                restarts += 1
                logger.warning(
                    "Load generator episode ended before sending traffic (%s); restart %d/%d in %.0fs",
                    reason,
                    restarts,
                    max_restarts,
                    restart_delay_s,
                )
                time.sleep(restart_delay_s)
                self.restart()
                continue
            try:
                start = self.start_episode()
            except Exception as exc:
                last_error = exc
            time.sleep(interval_s)
        detail = f"last error: {last_error}" if last_error is not None else self._describe(status)
        if start is not None:
            detail += f"; last episode-start response: {json.dumps(start)}"
        raise RuntimeError(
            f"load generator produced no traffic within {timeout_s:.0f}s ({detail}); recent logs:\n{self.logs()}"
        )

    @staticmethod
    def _describe(status: dict) -> str:
        done = status.get("episode_done")
        parts = [f"episode_done={json.dumps(done)}" if done is not None else "episode still starting"]
        tail = status.get("log_tail")
        if tail:
            parts.append("sidecar log tail:\n" + "\n".join(tail[-15:]))
        return "; ".join(parts)

    def summary(self, since_s: float | None, latency_percentile: float = 99.0, settle_s: float = 0.0) -> dict:
        """Aggregate every arrival sent after ``since_s`` (see the ledger module)."""
        since = "none" if since_s is None else repr(float(since_s))
        result = self._run_ledger("summary", since, repr(float(latency_percentile)), repr(float(settle_s)))
        if "error" in result:
            raise RuntimeError(f"load generator ledger unavailable: {result}")
        return result

    # ------------------------------------------------------------------ WorkloadManager API
    def collect(self, number=100, since_seconds=None) -> list[WorkloadEntry]:
        latest = self.latest_sent_s()
        since = None if latest is None or since_seconds is None else latest - float(since_seconds)
        return [self._as_entry(self.summary(since))]

    def recent_entries(self, duration=30) -> list[WorkloadEntry]:
        return self.collect(since_seconds=duration)

    @staticmethod
    def _as_entry(summary: dict) -> WorkloadEntry:
        offered = int(summary.get("offered") or 0)
        failures = int(summary.get("failures") or 0)
        return WorkloadEntry(
            time=time.time(),
            number=offered,
            log=json.dumps({k: summary.get(k) for k in ("offered", "failures", "error_rate", "goodput_ratio")}),
            ok=offered > 0 and failures == 0,
        )
