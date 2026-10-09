"""Bounded execution evidence for real application noise, independent of grading."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from sregym.conductor.scenarios.database_recovery import NoisePlan


@dataclass(frozen=True)
class NoiseExecution:
    event_id: str
    target: str
    operation: str
    requested_at_seconds: int
    admitted: bool
    status: str
    started_at_ns: int | None = None
    finished_at_ns: int | None = None
    measurements_json: str | None = None
    reason: str | None = None


class NoiseExecutor:
    def __init__(self, plan: NoisePlan, execute, evidence_path: Path, *, max_concurrent=2, horizon_seconds=None):
        if type(plan) is not NoisePlan or type(max_concurrent) is not int or max_concurrent < 1:
            raise ValueError("Noise needs a validated plan and positive execution bound")
        self.plan, self.execute, self.evidence_path = plan, execute, evidence_path
        self.max_concurrent = max_concurrent
        if horizon_seconds is not None and (type(horizon_seconds) is not int or not 15 <= horizon_seconds <= 86400):
            raise ValueError("Noise coverage horizon must be bounded to 15..86400 seconds")
        self.horizon_seconds, self.started_at = horizon_seconds, None
        self.cancel = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._receipts = {}
        self.runner_error = None
        self.completed = threading.Event()
        evidence_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def receipts(self):
        with self._lock:
            return tuple(
                self._receipts[decision.event_id]
                for decision in self.plan.decisions
                if decision.event_id in self._receipts
            )

    def record(self, receipt):
        with self._lock:
            self._receipts[receipt.event_id] = receipt
            with self.evidence_path.open("a", encoding="utf-8") as output:
                os.chmod(self.evidence_path, 0o600)
                output.write(json.dumps(asdict(receipt), sort_keys=True, separators=(",", ":")) + "\n")
                output.flush()
                os.fsync(output.fileno())

    def _run_event(self, decision):
        started = time.time_ns()
        status, measurements, reason = "failed", None, None
        try:
            if self.cancel.is_set():
                status = "cancelled"
            else:
                measurements = self.execute(decision.event, self.cancel)
                if type(measurements) is not dict or not measurements:
                    raise RuntimeError("Noise action did not return actual measurements")
                measurements = json.dumps(measurements, sort_keys=True, separators=(",", ":"), allow_nan=False)
                status = "cancelled" if self.cancel.is_set() else "executed"
        except Exception as exc:
            reason = type(exc).__name__
        self.record(
            NoiseExecution(
                decision.event_id,
                decision.event.target,
                decision.event.operation,
                decision.event.at_second,
                True,
                status,
                started,
                time.time_ns(),
                measurements,
                reason,
            )
        )

    def run(self):
        try:
            self._run_schedule()
        except BaseException as error:
            self.runner_error = type(error).__name__
            raise
        else:
            self.completed.set()

    def assert_available(self):
        if self.runner_error is not None or self.cancel.is_set():
            raise RuntimeError("A required real-noise owner is unavailable")
        if self._thread is not None and not self._thread.is_alive() and not self.completed.is_set():
            raise RuntimeError("A required real-noise schedule exited unexpectedly")
        receipts = self.receipts
        if any(receipt.admitted and receipt.status != "executed" for receipt in receipts):
            raise RuntimeError("A required real-noise action failed or exceeded its execution budget")
        if self.completed.is_set() and {receipt.event_id for receipt in receipts} != {
            decision.event_id for decision in self.plan.decisions
        }:
            raise RuntimeError("A required real-noise schedule has incomplete evidence")

    def _run_schedule(self):
        started = time.monotonic()
        self.started_at = started
        active = []
        with ThreadPoolExecutor(max_workers=self.max_concurrent, thread_name_prefix="application-noise") as pool:
            for decision in self.plan.decisions:
                if not decision.admitted:
                    self.record(
                        NoiseExecution(
                            decision.event_id,
                            decision.event.target,
                            decision.event.operation,
                            decision.event.at_second,
                            False,
                            "rejected",
                            reason=decision.rejection_reason,
                        )
                    )
                    continue
                remaining = started + decision.event.at_second - time.monotonic()
                if remaining > 0 and self.cancel.wait(remaining):
                    break
                if self.cancel.is_set():
                    break
                for future in active:
                    if future.done():
                        future.result()
                active = [future for future in active if not future.done()]
                if len(active) >= self.max_concurrent:
                    self.record(
                        NoiseExecution(
                            decision.event_id,
                            decision.event.target,
                            decision.event.operation,
                            decision.event.at_second,
                            True,
                            "not-executed",
                            reason="runtime-cap",
                        )
                    )
                else:
                    active.append(pool.submit(self._run_event, decision))
            for future in active:
                future.result()
        recorded = {receipt.event_id for receipt in self.receipts}
        for decision in self.plan.decisions:
            if decision.event_id not in recorded:
                self.record(
                    NoiseExecution(
                        decision.event_id,
                        decision.event.target,
                        decision.event.operation,
                        decision.event.at_second,
                        decision.admitted,
                        "cancelled",
                        reason="run-stopped",
                    )
                )

    def start(self):
        if self._thread or self.cancel.is_set():
            raise RuntimeError("Noise executor is single-use")
        self._thread = threading.Thread(target=self.run, name="application-noise-schedule", daemon=True)
        self._thread.start()

    def coverage_facts(self):
        """Owner policy coverage and executed dose; completion is not a crash."""
        self.assert_available()
        enabled = bool(self.plan.admitted)
        elapsed = None if self.started_at is None else time.monotonic() - self.started_at
        if enabled and self.horizon_seconds is not None and (elapsed is None or elapsed > self.horizon_seconds):
            raise RuntimeError("Real-noise policy does not cover the current observation window")
        receipts = self.receipts
        return {
            "enabled": enabled,
            "elapsed_seconds": elapsed,
            "horizon_seconds": self.horizon_seconds,
            "admitted_events": len(self.plan.admitted),
            "executed_events": sum(row.admitted and row.status == "executed" for row in receipts),
        }

    def stop(self, *, timeout=60):
        self.cancel.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise RuntimeError("Noise actions exceeded their cancellation deadline")

    def assert_completed(self):
        """Retain real execution/persistence failures after cooperative drain."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Real-noise owner has not drained")
        if not self.cancel.is_set() and not self.completed.is_set():
            raise RuntimeError("Real-noise owner has not completed")
        if self.runner_error is not None or any(
            row.admitted and row.status in {"failed", "not-executed"} for row in self.receipts
        ):
            raise RuntimeError("Real-noise execution or evidence failed before owner drain completed")
