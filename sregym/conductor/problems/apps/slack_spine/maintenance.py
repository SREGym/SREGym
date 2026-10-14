"""Maintenance for Slack Spine."""

from __future__ import annotations

import json

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.apps.slack_spine.base import SlackSpineProblem
from sregym.conductor.problems.apps.slack_spine.components import SlackLeg
from sregym.service.apps.slack_spine.application import SlackSpine
from sregym.service.apps.slack_spine.configuration import in_safe_maintenance_window


class MaintenanceSchedule(SlackLeg):
    """The durable PostgreSQL checkpoint schedule moved into the write peaks.

    The controller (db pod sidecar, ``db-maintenance:8081``) keeps its schedule
    in PostgreSQL and runs real dirty-write + CHECKPOINT cycles every 60s at
    ``offset_s`` from the load generator's epoch. Offset 35 lands inside each
    20-second write peak; the healthy schedule runs at 55.
    """

    component = "db/db.maintenance-controller"
    FAULT_OFFSET = 35
    HEALTHY = {"enabled": True, "period_s": 60, "offset_s": 55, "duration_s": 8}

    def __init__(self) -> None:
        super().__init__()
        self.completed_at: dict[str, int] = {}

    def deploy_values(self):
        return {
            "components": {"maintenanceController": {"enabled": True}},
            "maintenanceController": {
                "enabled": True,
                "periodS": self.HEALTHY["period_s"],
                "offsetS": self.HEALTHY["offset_s"],
                "durationS": self.HEALTHY["duration_s"],
                "readinessProbeTimeoutSeconds": 15,
            },
            "resources": {
                "maintenanceController": {
                    "requests": {"cpu": "250m", "memory": "96Mi"},
                    "limits": {"cpu": "1000m", "memory": "192Mi"},
                }
            },
        }

    def _state(self) -> dict:
        status, body = self.slack.http(SlackSpine.MAINTENANCE_URL)
        if status != 200:
            raise RuntimeError(f"maintenance API returned {status}: {body[:200]}")
        return json.loads(body)

    def _put(self, schedule: dict) -> None:
        status, body = self.slack.http(SlackSpine.MAINTENANCE_URL, method="PUT", body=schedule)
        if status != 200:
            raise RuntimeError(f"maintenance PUT returned {status}: {body[:200]}")

    def capture_baseline(self) -> None:
        self.completed_at["baseline"] = int(self._state().get("counters", {}).get("failed", 0))

    def inject(self) -> None:
        self._put({**self.HEALTHY, "offset_s": self.FAULT_OFFSET})

    def recover(self) -> None:
        self._put(self.HEALTHY)

    def checks(self, phase: str) -> list[CheckResult]:
        state = self._state()
        schedule = state.get("schedule", {})
        counters = state.get("counters", {})
        offset = float(schedule.get("offset_s", -1))
        results = [
            CheckResult(
                f"maintenance_offset_clear_of_peaks@{phase}", in_safe_maintenance_window(offset), detail=schedule
            ),
            CheckResult(
                f"maintenance_still_scheduled@{phase}",
                bool(schedule.get("enabled"))
                and float(schedule.get("period_s", 0)) == self.HEALTHY["period_s"]
                and float(schedule.get("duration_s", 0)) == self.HEALTHY["duration_s"],
                reason="unsafe_repair",
                detail=schedule,
            ),
            CheckResult(
                f"maintenance_runs_healthy@{phase}",
                int(counters.get("failed", 0)) <= self.completed_at.get("baseline", 0),
                reason="unsafe_repair",
                detail=counters,
            ),
        ]
        completed = int(counters.get("completed", 0))
        if phase == "declaration":
            self.completed_at["declaration"] = completed
        elif "declaration" in self.completed_at:
            results.append(
                CheckResult(
                    "maintenance_checkpoints_advanced",
                    completed > self.completed_at["declaration"],
                    reason="unsafe_repair",
                    detail={"at_declaration": self.completed_at["declaration"], "at_soak_end": completed},
                )
            )
        return results

    def describe(self) -> str:
        return (
            f"PUT {SlackSpine.MAINTENANCE_URL} offset_s={self.FAULT_OFFSET} (period 60s, duration 8s): durable "
            "checkpoints now start inside every 20-second write peak (healthy offset 55)."
        )


class SlackMaintenanceCollision(SlackSpineProblem):
    """Incident Arena 008: maintenance checkpoints scheduled inside the write peaks."""

    TASK = "008--slack-spine--06-F4-maintenance-collision-cc222870"
    PROBLEM_ID = "slack_maintenance_collision"

    def build_legs(self):
        return [MaintenanceSchedule()]
