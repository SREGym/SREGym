"""Base for Slack Spine."""

from __future__ import annotations

from sregym.conductor.oracles.apps.slack_spine.scope import SlackScopeGuard
from sregym.conductor.problems.apps.slack_spine.events import AdminEvent
from sregym.conductor.problems.apps.slack_spine.pools import RolePoolSize
from sregym.conductor.problems.contracts.incident_arena.base import IncidentArenaProblem
from sregym.service.apps.slack_spine.application import SlackSpine


class SlackSpineProblem(IncidentArenaProblem):
    GROUND_RULES = (
        "Ground rules from the incident runbook: repair the live system in place through its operational surfaces "
        "(each service's /admin API, the maintenance API, database administration, service configuration). The "
        "deployed application release is approved and must stay as it is. Restarting or rescaling workloads to mask "
        "a problem, raising capacity limits beyond the peer roles' budget, disabling safeguards or routine "
        "maintenance, and suppressing traffic are not acceptable."
    )
    #: PostgreSQL settings a repair may legitimately add (the task's allowed scope).
    ALLOWED_PG_SETTINGS: tuple[str, ...] = ()

    def create_app(self):
        return SlackSpine()

    def build_guards(self):
        owned = set()
        for leg in self.legs:
            if isinstance(leg, RolePoolSize):
                owned |= leg.owned_keys()
        restart_ok = {leg.role for leg in self.legs if isinstance(leg, RolePoolSize)}
        for leg in self.legs:
            if isinstance(leg, AdminEvent):
                leg.restart_allowed = restart_ok
        return [SlackScopeGuard(owned_keys=owned, allowed_settings=self.ALLOWED_PG_SETTINGS)]

    def deploy_values(self):
        # Per-task load generator sizing (the session-heavy profiles need more memory).
        resources = self.task.task_values.get("resources") or {}
        return {"resources": {"loadgen": resources["loadgen"]}} if "loadgen" in resources else {}
