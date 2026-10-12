"""Base for Frappe."""

from __future__ import annotations

from sregym.conductor.oracles.apps.frappe.scope import FrappeScopeGuard
from sregym.conductor.problems.apps.frappe.faults import MariaDBGlobal, SiteGrantRevocation
from sregym.conductor.problems.contracts.incident_arena.base import IncidentArenaProblem
from sregym.service.apps.frappe.application import Frappe


class FrappeProblem(IncidentArenaProblem):
    GROUND_RULES = (
        "Ground rules from the incident runbook: repair the live system in place through its operational "
        "surfaces (database administration, broker configuration, the toolbox helpers). Preserve queued work. "
        "Broad grants, flushing data, cache-style eviction, scaling workers, suppressing traffic, and "
        "restart-only workarounds are not acceptable."
    )
    # The queue legs fill or stall over minutes of traffic.
    PROPAGATION_S = 180

    def create_app(self):
        return Frappe()

    def build_guards(self):
        grant_legs = tuple(leg for leg in self.legs if isinstance(leg, SiteGrantRevocation))
        owned = tuple(leg.variable for leg in self.legs if isinstance(leg, MariaDBGlobal))
        return [FrappeScopeGuard(owned_globals=owned, grant_legs=grant_legs)]
