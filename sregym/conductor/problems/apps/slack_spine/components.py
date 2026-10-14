"""Shared database and rollout operations for Slack Spine fault legs."""

from __future__ import annotations

import json
import time

from sregym.conductor.problems.incident_arena import FaultLeg
from sregym.service.apps.slack_spine.application import SlackSpine

LEAK_AGE_S = 15
# Healthy transactions may hold this lock briefly; only long-idle holders count.
_HOLDERS = (
    "SELECT DISTINCT a.pid FROM pg_stat_activity a "
    "JOIN pg_locks l ON l.pid = a.pid JOIN pg_class c ON c.oid = l.relation "
    "WHERE a.state = 'idle in transaction' AND c.relname = 'channel_seq' AND a.pid <> pg_backend_pid() "
    "AND now() - a.state_change > interval '{age} seconds'"
)
LOCK_HOLDER_SQL = f"SELECT count(*) FROM ({_HOLDERS.format(age=LEAK_AGE_S)}) h"
TERMINATE_HOLDERS_SQL = f"SELECT count(pg_terminate_backend(pid)) FROM ({_HOLDERS.format(age=1)}) h"


class SlackLeg(FaultLeg):
    @property
    def slack(self) -> SlackSpine:
        return self.app

    def deployment_env(self, deployment: str) -> dict[str, str]:
        raw = self.slack.kubectl.exec_command_checked(
            f"kubectl get deployment {deployment} -n {self.namespace} -o json", timeout=60
        )
        containers = json.loads(raw)["spec"]["template"]["spec"]["containers"]
        return {e["name"]: e.get("value") for c in containers for e in c.get("env", []) if "name" in e}

    def holder_count(self) -> int:
        return int(self.slack.psql(LOCK_HOLDER_SQL).strip() or 0)

    def wait_for(self, predicate, timeout_s: float, interval_s: float = 5, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception:
                pass
            time.sleep(interval_s)
        raise RuntimeError(f"timed out after {timeout_s}s waiting for {what}")
