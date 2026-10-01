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

    def wait_for_traffic(self, timeout_s: float = 600, interval_s: float = 10) -> float:
        """Block until the ledger has arrivals; return the newest ``sent_s``."""
        deadline = time.monotonic() + timeout_s
        last_error = None
        while time.monotonic() < deadline:
            try:
                latest = self.latest_sent_s()
                if latest is not None:
                    return latest
            except Exception as exc:  # pod still starting
                last_error = exc
            time.sleep(interval_s)
        raise RuntimeError(f"load generator produced no traffic within {timeout_s}s (last error: {last_error})")

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
