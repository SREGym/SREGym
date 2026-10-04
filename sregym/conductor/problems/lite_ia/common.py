"""Shared plumbing for the SREGym-Lite problems ported to the Incident Arena apps.

SREGym-Lite's faults were written against Hotel Reservation, Social Network and
Astronomy Shop. The ports keep each fault's causal mechanism and its
state-based mitigation oracle, re-targeted at a component of Frappe, Saleor or
Slack Spine, and add one app-level requirement: after the agent finishes, the
chart's own load generator must see its traffic served about as well as it was
before the fault (:class:`LoadgenHealthOracle`).
"""

from __future__ import annotations

import logging
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.service.apps.incident_arena import Frappe, Saleor, SlackSpine

logger = logging.getLogger(__name__)

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


def is_job_pod(pod) -> bool:
    """True for pods owned by a Job (one-shot migrate/seed pods that end Succeeded)."""
    return any(ref.kind == "Job" for ref in (pod.metadata.owner_references or []))


class LoadgenHealthOracle(Oracle):
    """The app's own users are served about as well as before the fault.

    The baseline is the load generator's error rate over the healthy window
    before injection. At evaluation the oracle lets traffic settle for
    ``SOAK_S`` seconds and then measures a fresh ``WINDOW_S``-second window,
    so only traffic sent after the agent finished counts. That window must
    carry traffic and fail no more than
    ``max(ABS_CEILING, BASELINE_FACTOR * baseline + SLACK)``, capped at
    ``MAX_CEILING`` so a baseline taken during warm-up cannot excuse a broken app.
    """

    FAILURE_CLASSES = {
        "no_traffic": "agent_error",
        "error_rate_above_baseline": "agent_error",
        "loadgen_unavailable": "ambiguous",
    }
    WINDOW_S = 60.0
    SOAK_S = 30.0
    ABS_CEILING = 0.05
    BASELINE_FACTOR = 2.0
    SLACK = 0.02
    MAX_CEILING = 0.15

    def __init__(self, problem, window_s: float | None = None, soak_s: float | None = None):
        super().__init__(problem)
        self.window_s = float(window_s if window_s is not None else self.WINDOW_S)
        self.soak_s = float(soak_s if soak_s is not None else self.SOAK_S)
        self.baseline_error_rate: float | None = None

    @property
    def workload(self):
        app = self.problem.app
        if not hasattr(app, "wrk"):
            app.create_workload()
        return app.wrk

    def _window(self) -> dict:
        latest = self.workload.latest_sent_s()
        if latest is None:
            return {"offered": 0}
        return self.workload.summary(max(0.0, float(latest) - self.window_s))

    def capture_baseline(self) -> None:
        try:
            summary = self._window()
            self.baseline_error_rate = float(summary.get("error_rate") or 0.0)
            print(
                f"[LoadgenHealth] baseline: offered={summary.get('offered')} error_rate={self.baseline_error_rate:.4f}"
            )
        except Exception as exc:  # The ceiling falls back to ABS_CEILING.
            logger.warning("Could not capture load generator baseline: %s", exc)
            self.baseline_error_rate = None

    def ceiling(self) -> float:
        base = self.baseline_error_rate or 0.0
        return min(self.MAX_CEILING, max(self.ABS_CEILING, self.BASELINE_FACTOR * base + self.SLACK))

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Load generator health ==")
        try:
            start = self.workload.latest_sent_s()
            time.sleep(self.soak_s + self.window_s)
            since = None if start is None else float(start) + self.soak_s
            summary = self.workload.summary(since)
        except Exception as exc:
            print(f"❌ Load generator ledger unavailable: {exc}")
            return self.fail("loadgen_unavailable", error=str(exc))
        offered = int(summary.get("offered") or 0)
        error_rate = summary.get("error_rate")
        ceiling = self.ceiling()
        detail = {"offered": offered, "error_rate": error_rate, "ceiling": ceiling, "window_s": self.window_s}
        if offered == 0:
            print("❌ The load generator sent no traffic in the window")
            return self.fail("no_traffic", **detail)
        if error_rate is None or float(error_rate) > ceiling:
            print(f"❌ Error rate {error_rate} exceeds {ceiling:.3f} (baseline {self.baseline_error_rate})")
            return self.fail("error_rate_above_baseline", **detail)
        print(f"✅ {offered} requests, error rate {float(error_rate):.4f} <= {ceiling:.3f}")
        return {"success": True, **detail}


def with_app_health(problem, oracle: Oracle) -> CompoundedOracle:
    """Require ``oracle`` (the fault's own check) and recovered user traffic."""
    compound = CompoundedOracle(problem, fault=oracle, app_health=LoadgenHealthOracle(problem))
    # Several fault oracles wait minutes for rollouts or replay a trigger, and
    # the health window adds SOAK_S + WINDOW_S on top.
    compound.evaluation_timeout_seconds = max(1200.0, oracle.evaluation_timeout_seconds or 0.0)
    return compound
