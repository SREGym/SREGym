"""Mitigation oracle for the problems ported from Incident Arena.

Incident Arena grades a repair with two gates (abundant-ai/incident-arena,
``tests/verifier``):

* **outcome** -- client-measured health over a post-repair soak window: error
  rate, goodput, per-driver limits and (for most Slack/Saleor tasks) latency,
  with a repository-wide 20% tolerance on every band. Latency bands are
  absolute milliseconds from Incident Arena's hardware, so they are re-based
  on the healthy latency this cluster showed before injection when that is
  higher (``OutcomeSpec.latency_baseline``). Goodput includes dropped arrivals
  instead of conditioning success on the client accepting a request;
* **safe repair** -- white-box state: the injected cause is gone, the change
  stayed inside the allowed scope, and it survives a verifier-owned restart.

This oracle reproduces both against the live cluster. Problems contribute the
white-box checks (``problem.run_checks(phase)``) and durability challenges
(``problem.challenges()``); the outcome gate reads the chart's own load
generator ledger (:class:`sregym.generators.workload.incident_arena.IncidentArenaLoadgen`).

Order of evaluation mirrors the Incident Arena episode: declaration-time state
checks (failing fast), challenges, a soak window, soak-end state checks, then
the outcome gate over the soak.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

logger = logging.getLogger(__name__)

# Incident Arena: verifier/health_bands.py (reused-health-bands-v1-20pct).
CEILING_MULTIPLIER = 1.20
FLOOR_MULTIPLIER = 0.80


def relaxed_ceiling(value: float, maximum: float | None = None) -> float:
    result = float(value) * CEILING_MULTIPLIER
    return min(maximum, result) if maximum is not None else result


def relaxed_floor(value: float) -> float:
    return max(0.0, float(value) * FLOOR_MULTIPLIER)


@dataclass
class CheckResult:
    """One graded assertion. ``reason`` is the failure code used when it fails."""

    name: str
    passed: bool
    reason: str = "fault_still_present"
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "reason": self.reason, "detail": self.detail}


# A latency band is never tighter than this multiple of the healthy p99 that the
# same cluster showed before injection (see ``OutcomeSpec.latency_baseline``).
LATENCY_BASELINE_FACTOR = 2.0


@dataclass(frozen=True)
class OutcomeSpec:
    """Client-side health bands for the soak window (Incident Arena ``thresholds``).

    Incident Arena calibrated its latency bands in absolute milliseconds on its
    own hardware (8 vCPU, single-node k3s). ``latency_baseline`` holds the
    healthy p99 per phase kind (``overall``) and per driver (``by_driver``)
    measured on this cluster before injection; each latency band becomes
    ``max(band, latency_baseline_factor * healthy p99)`` before the 20%
    tolerance. Error-rate and goodput bands stay absolute.
    """

    thresholds: dict[str, Any]
    gate_latency: bool = True
    soak_s: float = 180.0
    latency_baseline: dict[str, Any] | None = None
    latency_baseline_factor: float = LATENCY_BASELINE_FACTOR
    completion_timeout_s: float = 0.0

    @property
    def latency_percentile(self) -> float:
        return float(self.thresholds.get("latency_percentile", 99.0))

    @property
    def settle_s(self) -> float:
        return float(self.thresholds.get("latency_settle_s", 0.0))


def evaluate_outcome(summary: dict[str, Any], spec: OutcomeSpec) -> list[CheckResult]:
    """Grade a ledger summary against the task's bands (pure; unit-tested)."""
    thresholds = spec.thresholds
    offered = int(summary.get("offered") or 0)
    results = [
        CheckResult(
            "traffic_observed",
            offered > 0,
            reason="no_traffic_observed",
            detail={"offered": offered, "dropped": summary.get("dropped")},
        )
    ]
    if offered == 0:
        return results

    if "error_rate_max" in thresholds:
        limit = relaxed_ceiling(thresholds["error_rate_max"], maximum=1.0)
        value = summary.get("error_rate")
        results.append(
            CheckResult(
                "sustained_error_rate",
                value is not None and value <= limit,
                reason="service_unhealthy",
                detail={"value": value, "limit": limit, "failures": summary.get("failures")},
            )
        )
    if "goodput_min_ratio" in thresholds:
        limit = relaxed_floor(thresholds["goodput_min_ratio"])
        value = _scheduled_goodput(summary)
        results.append(
            CheckResult(
                "sustained_correct_goodput",
                value is not None and value >= limit,
                reason="service_unhealthy",
                detail={
                    "value": value,
                    "limit": limit,
                    "good": summary.get("good"),
                    "dropped": summary.get("dropped", 0),
                },
            )
        )

    by_driver = summary.get("by_driver") or {}
    for driver, limits in sorted((thresholds.get("by_driver") or {}).items()):
        stats = by_driver.get(driver) or {}
        detail: dict[str, Any] = {"offered": stats.get("offered", 0)}
        passed = bool(stats.get("offered"))
        if passed and "goodput_min_ratio" in limits:
            floor = relaxed_floor(limits["goodput_min_ratio"])
            value = _scheduled_goodput(stats)
            detail["goodput"] = {"value": value, "limit": floor, "dropped": stats.get("dropped", 0)}
            passed = passed and value is not None and value >= floor
        if passed and "error_rate_max" in limits:
            ceiling = relaxed_ceiling(limits["error_rate_max"], maximum=1.0)
            value = stats.get("error_rate")
            detail["error_rate"] = {"value": value, "limit": ceiling}
            passed = (value if value is not None else 1.0) <= ceiling
        results.append(CheckResult(f"driver_{driver}_within_limits", passed, reason="service_unhealthy", detail=detail))

    if spec.gate_latency:
        results.extend(_latency_checks(summary, spec))
    return results


def _latency_checks(summary: dict[str, Any], spec: OutcomeSpec) -> list[CheckResult]:
    baseline = spec.latency_baseline or {}
    results = [
        _latency_check(
            "sustained_latency",
            summary.get("latency") or {},
            spec.thresholds.get("p99_ms_by_phase") or {},
            baseline.get("overall") or {},
            spec.latency_baseline_factor,
        )
    ]
    by_driver = summary.get("by_driver") or {}
    for driver, bands in sorted((spec.thresholds.get("latency_by_driver") or {}).items()):
        stats = (by_driver.get(driver) or {}).get("latency") or {}
        healthy = (baseline.get("by_driver") or {}).get(driver) or {}
        results.append(_latency_check(f"driver_{driver}_latency", stats, bands, healthy, spec.latency_baseline_factor))
    return results


def _scheduled_goodput(stats: dict[str, Any]) -> float | None:
    """Correct responses divided by all recorded arrivals, including drops."""
    scheduled = int(stats.get("offered") or 0) + int(stats.get("dropped") or 0)
    return int(stats.get("good") or 0) / scheduled if scheduled else None


def _latency_check(
    name: str,
    latency: dict[str, Any],
    bands: dict[str, Any],
    healthy: dict[str, Any] | None = None,
    factor: float = LATENCY_BASELINE_FACTOR,
) -> CheckResult:
    per_kind = {}
    passed = True
    missing = []
    for kind, band in sorted(bands.items()):
        stats = latency.get(kind) or {}
        value = stats.get("p_ms")
        if value is None or not stats.get("n"):
            missing.append(kind)
            per_kind[kind] = {"n": stats.get("n", 0), "value": value, "pass": False}
            passed = False
            continue
        healthy_p = (healthy or {}).get(kind)
        effective = max(float(band), factor * float(healthy_p)) if healthy_p is not None else float(band)
        limit = relaxed_ceiling(effective)
        ok = value <= limit
        passed = passed and ok
        per_kind[kind] = {
            "n": stats.get("n"),
            "value": value,
            "band": band,
            "healthy": healthy_p,
            "limit": limit,
            "pass": ok,
        }
    return CheckResult(
        name,
        passed and bool(bands),
        reason="no_traffic_observed" if missing or not bands else "service_unhealthy",
        detail=per_kind,
    )


class IncidentArenaMitigationOracle(Oracle):
    """Grade an Incident Arena repair: root cause removed, safely, durably, and healthy."""

    # Includes durability challenges, the soak, and bounded request completion.
    evaluation_timeout_seconds = 1800

    # "fault_still_present" (the injected cause, read directly from the component
    # that holds it) uses the shared AGENT_ERROR classification.
    FAILURE_CLASSES = {
        # The repair broadened scope: a guarded setting, privilege or workload changed.
        "unsafe_repair": FailureClass.AGENT_ERROR,
        # The cause was masked by restarting the workload that held it.
        "restart_masked_fault": FailureClass.AGENT_ERROR,
        # The repair did not survive the verifier-owned restart challenge.
        "repair_not_durable": FailureClass.AGENT_ERROR,
        # Client-measured health stayed outside the task's bands. The cause can
        # be collateral damage from the agent or an unhealthy node.
        "service_unhealthy": FailureClass.AMBIGUOUS,
        "challenge_failed": FailureClass.AMBIGUOUS,
        # The load generator recorded nothing in the soak window.
        "no_traffic_observed": FailureClass.AMBIGUOUS,
    }

    # Healthy traffic measured before injection to calibrate the latency bands.
    LATENCY_BASELINE_WINDOW_S = 180.0

    def __init__(self, problem):
        super().__init__(problem)
        self.results: list[CheckResult] = []
        self.latency_baseline: dict[str, Any] | None = None

    def capture_baseline(self) -> None:
        self.problem.capture_baseline()
        spec: OutcomeSpec = self.problem.outcome_spec()
        if not spec.gate_latency or not getattr(self.problem, "HEALTHY_BASELINE", True):
            return
        try:
            self.latency_baseline = self._measure_latency_baseline(spec)
        except Exception as exc:
            if spec.completion_timeout_s:
                raise RuntimeError("Could not capture a complete healthy latency window") from exc
            logger.warning("Could not measure the healthy latency; grading against absolute bands", exc_info=True)
            self.latency_baseline = None
        logger.info("Healthy latency baseline (p%.0f, ms): %s", spec.latency_percentile, self.latency_baseline)

    def _measure_latency_baseline(self, spec: OutcomeSpec) -> dict[str, Any]:
        """Healthy p-latency per phase kind and per driver over the last baseline window."""
        workload = self.problem.app.wrk
        window = max(float(spec.soak_s), self.LATENCY_BASELINE_WINDOW_S)
        latest = workload.wait_for_traffic()
        if latest < window:
            # Too little healthy traffic yet (the validator injects right after deploy).
            time.sleep(window - latest)
            latest = workload.latest_sent_s() or latest
        options = {"latency_percentile": spec.latency_percentile, "settle_s": 0.0}
        if spec.completion_timeout_s:
            state = workload.status()
            latest = state.get("latest_sent_s")
            episode = state.get("episode_id")
            if latest is None or latest < window or episode is None:
                raise RuntimeError("Healthy workload window is incomplete")
            self._wait_for_completions(spec, episode, latest)
            options["until_s"] = latest
        summary = workload.summary(latest - window, **options)
        missing = [check.name for check in _latency_checks(summary, spec) if check.reason == "no_traffic_observed"]
        if missing:
            raise RuntimeError(f"Healthy latency window is missing required samples: {', '.join(missing)}")

        def p_ms(stats: dict[str, Any]) -> dict[str, Any]:
            return {
                kind: v.get("p_ms") for kind, v in (stats.get("latency") or {}).items() if v.get("p_ms") is not None
            }

        return {
            "overall": p_ms(summary),
            "by_driver": {driver: p_ms(stats) for driver, stats in (summary.get("by_driver") or {}).items()},
        }

    def _wait_for_completions(self, spec: OutcomeSpec, episode: str, until_s: float) -> None:
        """Drain a closed arrival cohort without accepting a replacement episode."""
        time.sleep(spec.completion_timeout_s)
        completed = self.problem.app.wrk.status()
        current = completed.get("latest_sent_s")
        if completed.get("episode_id") != episode or current is None or current < until_s:
            raise RuntimeError("Workload restarted before the measurement window completed")

    def _verdict(self, results: list[CheckResult]) -> dict:
        self.results = results
        failed = [r for r in results if not r.passed]
        for r in results:
            print(f"{'✅' if r.passed else '❌'} {r.name}: {r.detail}")
        if not failed:
            return {"success": True, "checks": [r.as_dict() for r in results]}
        first = failed[0]
        return self.fail(
            first.reason,
            failed_checks=[r.name for r in failed],
            checks=[r.as_dict() for r in results],
        )

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Mitigation Evaluation (Incident Arena) ==")
        problem = self.problem
        spec: OutcomeSpec = replace(problem.outcome_spec(), latency_baseline=self.latency_baseline)
        results: list[CheckResult] = []

        results.extend(problem.run_checks("declaration"))
        if any(not r.passed for r in results):
            return self._verdict(results)

        for challenge in problem.challenges():
            results.append(challenge())
        if any(not r.passed for r in results):
            return self._verdict(results)

        workload = problem.app.wrk
        try:
            # Normally one ledger read. A load generator pod that restarted
            # waits for its episode to be started again before it sends.
            mark = workload.wait_for_traffic()
            episode = None
            if spec.completion_timeout_s:
                state = workload.status()
                mark, episode = state.get("latest_sent_s"), state.get("episode_id")
                if mark is None or episode is None:
                    raise RuntimeError("Workload measurement has no active episode")
        except Exception as exc:
            results.append(
                CheckResult("traffic_observed", False, reason="no_traffic_observed", detail={"error": str(exc)})
            )
            return self._verdict(results)
        logger.info("Soaking for %.0fs from load generator mark %s", spec.soak_s, mark)
        time.sleep(spec.soak_s)
        try:
            latest = workload.latest_sent_s()
            options = {"latency_percentile": spec.latency_percentile, "settle_s": spec.settle_s}
            if spec.completion_timeout_s:
                if episode is None or latest is None or latest < mark:
                    raise RuntimeError("Workload clock regressed during measurement")
                self._wait_for_completions(spec, episode, latest)
                options["until_s"] = latest
            results.extend(problem.run_checks("soak_end"))
            # Zero-bound legacy workloads do not identify their client episodes.
            since = mark if latest is not None and latest >= mark else None
            summary = workload.summary(since, **options)
        except Exception as exc:
            results.append(
                CheckResult("workload_window_intact", False, reason="no_traffic_observed", detail={"error": str(exc)})
            )
            return self._verdict(results)
        results.extend(evaluate_outcome(summary, spec))
        return self._verdict(results)
