"""Shared plumbing for the SREGym-Lite problems ported to the Incident Arena apps.

SREGym-Lite's faults were written against Hotel Reservation, Social Network and
Astronomy Shop. The ports keep each fault's causal mechanism and its
state-based mitigation oracle, re-targeted at a component of Frappe, Saleor or
Slack Spine, and add one app-level requirement: after the agent finishes, the
chart's own load generator must see its traffic served about as well as it was
before the fault (:class:`LoadgenHealthOracle`).
"""

from __future__ import annotations

import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.incident_arena import OutcomeSpec, evaluate_outcome
from sregym.service.apps.frappe.application import Frappe
from sregym.service.apps.saleor.application import Saleor
from sregym.service.apps.slack_spine.application import SlackSpine

APPS = {"frappe": Frappe, "saleor": Saleor, "slack_spine": SlackSpine}

# Fault-free profiles compiled into each app's load generator, looped for the
# whole run (``continuous_load_profile`` sets ``loop`` and the deadline). Frappe
# and Saleor use the bases of Incident Arena tasks 000 and 006; Slack Spine uses
# the chart's default session profile, whose drivers touch nearly every role
# (login, history, unread, search, threads, presence, posts, files).
LOAD_PROFILES = {
    "frappe": ("lite_frappe", {"base": "frappe_jobs", "soak_cycles": 3}),
    "saleor": ("lite_saleor", {"base": "saleor_eval", "soak_cycles": 4}),
    "slack_spine": ("lite_slack", {"base": "slack_session", "soak_cycles": 2}),
}

# Deploy-time chart values every port of an app needs. Frappe's pods share a
# ReadWriteOnce volume that is node-local on kind, and the chart's default
# hostname spread is DoNotSchedule: a rolling update's surge pod must join the
# volume's node yet may not, so no Deployment rollout can ever finish. Faults
# and their fixes both roll Deployments, so the spread becomes best-effort.
DEPLOY_VALUES = {
    "frappe": {
        "erpnext": {
            "nginx": {"defaultTopologySpread": {"whenUnsatisfiable": "ScheduleAnyway"}},
            "worker": {"defaultTopologySpread": {"whenUnsatisfiable": "ScheduleAnyway"}},
        }
    },
}

# Healthy traffic before injection; the health oracle's baseline comes from it.
BASELINE_S = 120
PROPAGATION_S = 60


def make_app(app_name: str):
    """Build an Incident Arena app with its continuous, fault-free load profile."""
    if app_name not in APPS:
        raise ValueError(f"Unsupported app name: {app_name} (expected one of {sorted(APPS)})")
    app = APPS[app_name]()
    app.set_load_profile(*LOAD_PROFILES[app_name])
    app.configure(DEPLOY_VALUES.get(app_name, {}))
    return app


class LoadgenHealthOracle(Oracle):
    """Compare a complete post-repair traffic window with the healthy baseline."""

    FAILURE_CLASSES = {
        "service_unhealthy": "ambiguous",
        "no_traffic_observed": "ambiguous",
        "loadgen_unavailable": "ambiguous",
    }
    WINDOW_S = 120.0
    SOAK_S = 30.0
    ABS_CEILING = 0.05
    BASELINE_FACTOR = 2.0
    SLACK = 0.02
    MAX_CEILING = 0.15

    def __init__(self, problem, window_s: float | None = None, soak_s: float | None = None):
        super().__init__(problem)
        self.window_s = float(window_s if window_s is not None else self.WINDOW_S)
        self.soak_s = float(soak_s if soak_s is not None else self.SOAK_S)
        self.baseline: dict | None = None

    @property
    def workload(self):
        app = self.problem.app
        if not hasattr(app, "wrk"):
            app.create_workload()
        return app.wrk

    def _state(self, *, episode: str | None = None, minimum: float = 0.0) -> dict:
        state = self.workload.status()
        latest = state.get("latest_sent_s")
        current_episode = state.get("episode_id")
        if latest is None or current_episode is None or float(latest) < minimum:
            raise RuntimeError("The workload measurement window is incomplete")
        if episode is not None and current_episode != episode:
            raise RuntimeError("The workload restarted during the measurement window")
        return state

    def _completed_window(self, since: float, until: float, episode: str) -> dict:
        # The pinned application declares the longest possible request path.
        # Late completion records must join the closed arrival cohort.
        time.sleep(self.problem.app.WORKLOAD_COMPLETION_TIMEOUT_S)
        self._state(episode=episode, minimum=until)
        return self.workload.summary(since, until_s=until)

    @classmethod
    def _limits(cls, stats: dict) -> dict:
        ceiling = min(cls.MAX_CEILING, max(cls.ABS_CEILING, cls.BASELINE_FACTOR * stats["error_rate"] + cls.SLACK))
        return {"error_rate_max": ceiling, "goodput_min_ratio": 1.0 - ceiling}

    @staticmethod
    def _latency_bands(stats: dict) -> dict:
        return {
            kind: max(1000.0, 2.0 * value["p_ms"])
            for kind, value in (stats.get("latency") or {}).items()
            if value.get("n") and value.get("p_ms") is not None
        }

    def _spec(self, summary: dict) -> OutcomeSpec:
        baseline = self.baseline
        if baseline is None:
            raise RuntimeError("The healthy workload baseline is missing")
        drivers = baseline.get("by_driver") or {}
        # A file download exists only after a session chooses an uploaded file;
        # it is not a periodic driver. Grade it when present, not by absence.
        intermittent = {"session_file_download"} if self.problem.app.app_id == "slack-spine" else set()
        required = set(drivers) - intermittent
        observed = set(summary.get("by_driver") or {})
        thresholds = {
            **self._limits(baseline),
            "p99_ms_by_phase": self._latency_bands(baseline),
            "by_driver": {name: self._limits(drivers.get(name) or baseline) for name in required | observed},
        }
        return OutcomeSpec(thresholds, tolerance=0.0)

    def capture_baseline(self) -> None:
        state = self._state(minimum=self.window_s)
        until = float(state["latest_sent_s"])
        self.baseline = self._completed_window(until - self.window_s, until, state["episode_id"])
        checks = evaluate_outcome(self.baseline, self._spec(self.baseline))
        if not self.baseline.get("by_driver") or any(not check.passed for check in checks):
            self.baseline = None
            raise RuntimeError("The pre-injection workload baseline is not healthy and complete")
        print(
            f"[LoadgenHealth] baseline: {self.baseline['offered']} requests; error_rate={self.baseline['error_rate']}"
        )

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Load generator health ==")
        try:
            first = self._state()
            time.sleep(self.soak_s)
            start = self._state(episode=first["episode_id"], minimum=float(first["latest_sent_s"]))
            time.sleep(self.window_s)
            end = self._state(episode=start["episode_id"], minimum=float(start["latest_sent_s"]) + self.window_s * 0.9)
            summary = self._completed_window(
                float(start["latest_sent_s"]), float(end["latest_sent_s"]), start["episode_id"]
            )
            checks = evaluate_outcome(summary, self._spec(summary))
        except Exception as exc:
            print(f"❌ Load generator ledger unavailable: {exc}")
            return self.fail("loadgen_unavailable", error=str(exc))
        detail = {"summary": summary, "checks": [check.as_dict() for check in checks]}
        for check in checks:
            print(f"{'✅' if check.passed else '❌'} {check.name}: {check.detail}")
        failed = next((check for check in checks if not check.passed), None)
        if failed:
            return self.fail(failed.reason, **detail)
        return {"success": True, **detail}


def with_app_health(problem, oracle: Oracle) -> CompoundedOracle:
    """Require ``oracle`` (the fault's own check) and recovered user traffic."""
    compound = CompoundedOracle(problem, fault=oracle, app_health=LoadgenHealthOracle(problem))
    # Several fault oracles wait minutes for rollouts or replay a trigger, and
    # the health window adds SOAK_S + WINDOW_S on top.
    compound.evaluation_timeout_seconds = max(1200.0, oracle.evaluation_timeout_seconds or 0.0)
    return compound
