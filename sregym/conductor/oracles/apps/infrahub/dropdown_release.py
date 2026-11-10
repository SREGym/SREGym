"""Every released Dropdown choice must be in the stored schema, served by every API worker and usable.

The check reads the NetworkDevice schema many times over fresh connections, so
every gunicorn worker answers, and requires one identical set of choices that
holds every choice the kind had before the incident plus every released one,
with the attributes still Dropdowns. It then creates and reads back devices with
every released and pre-existing choice (what inventory-sync needs). Taking a
released value out of inventory-sync's configuration (turning the release off),
dropping a pre-existing choice, or turning a Dropdown into free text fails, as do
the platform invariants (topology, volumes, pre-incident objects). Restarting
infrahub-server does not restore anything (the stored schema lacks the choice)
and is reported only as collateral. Every check runs and is reported.
"""

import json

from sregym.conductor.oracles.apps.infrahub.platform import InfrahubPlatformOracle
from sregym.conductor.oracles.failure import FailureClass


class InfrahubDropdownReleaseOracle(InfrahubPlatformOracle):
    FAILURE_CLASSES = {
        "release_turned_off": FailureClass.AGENT_ERROR,
        "dropdown_attribute_changed": FailureClass.AGENT_ERROR,
        "existing_choice_removed": FailureClass.AGENT_ERROR,
        "released_choice_missing": FailureClass.AGENT_ERROR,
        "api_workers_disagree_on_schema": FailureClass.AGENT_ERROR,
        "device_with_released_choice_rejected": FailureClass.AGENT_ERROR,
    }
    schema_samples = 32
    rounds = 2

    def __init__(self, problem, original, released):
        super().__init__(problem)
        #: {attribute: [choices]} before the incident, and the choices each team released.
        self.original = {a: list(c) for a, c in original.items()}
        self.released = {a: list(c) for a, c in released.items()}

    @property
    def wanted(self):
        """Choices every worker must serve: the original ones, plus the released ones once released."""
        wanted = {a: set(c) for a, c in self.original.items()}
        if getattr(self.problem, "release_started", False):
            for attribute, choices in self.released.items():
                wanted[attribute] |= set(choices)
        return wanted

    def release_check(self):
        if not getattr(self.problem, "release_started", False):
            return {"ok": True, "released": False}
        configured = (self.app.configured_inventory_fields() or {}).get("values", {})
        dropped = {a: sorted(set(c) - set(configured.get(a, []))) for a, c in self.released.items()}
        dropped = {a: c for a, c in dropped.items() if c}
        fields = (self.app.configured_inventory_fields() or {}).get("NetworkDevice", [])
        unset = sorted(a for a in self.released if a not in fields)
        return {"ok": not dropped and not unset, "removed_values": dropped, "fields_not_set": unset}

    def schema_check(self):
        wanted = self.wanted
        rounds = []
        for _ in range(self.rounds):
            views = self.app.run_client("choice_views", samples=self.schema_samples, exec_timeout=600)
            rounds.append(views)
        served = {}
        for views in rounds:
            for view, count in views.items():
                served[view] = served.get(view, 0) + count
        result = {"views": served}
        decoded = [json.loads(v) for v in served]
        not_dropdown = sorted({a for view in decoded for a in wanted if a not in view})
        removed = sorted(
            {
                f"{a}={c}"
                for view in decoded
                for a in self.original
                if a in view
                for c in self.original[a]
                if c not in view[a]
            }
        )
        missing = sorted(
            {f"{a}={c}" for view in decoded for a in wanted if a in view for c in wanted[a] if c not in view[a]}
            - set(removed)
        )
        result.update(
            attribute_no_longer_dropdown=not_dropdown,
            existing_choices_removed=removed,
            released_choices_missing=missing,
            workers_agree=len(served) == 1,
        )
        result["ok"] = not (not_dropdown or removed or missing) and len(served) == 1
        return result

    def probe_check(self):
        wanted = {a: sorted(c) for a, c in self.wanted.items()}
        probe = self.app.run_client("probe_choices", values=wanted, exec_timeout=600)
        bad = [r for r in probe["results"] if not r["ok"]]
        return {"ok": probe["ok"], "probed": len(probe["results"]), "rejected": bad[:4]}

    def evaluate(self):
        if not self.baseline:
            return self.fail("baseline_not_captured")
        checks = {}
        self.platform_checks(checks)
        self.run_check(checks, "release_on", self.release_check)
        self.run_check(checks, "schema", self.schema_check)
        self.run_check(checks, "probe_devices", self.probe_check)
        schema = checks.get("schema", {})
        if schema.get("ok") is False:
            # Name the schema failure precisely in the reason.
            if schema.get("attribute_no_longer_dropdown"):
                checks["schema"]["why"] = "dropdown_attribute_changed"
            elif schema.get("existing_choices_removed"):
                checks["schema"]["why"] = "existing_choice_removed"
            elif schema.get("released_choices_missing"):
                checks["schema"]["why"] = "released_choice_missing"
            else:
                checks["schema"]["why"] = "api_workers_disagree_on_schema"
        order = [
            *self.PLATFORM_ORDER,
            ("release_on", "release_turned_off"),
            ("schema", schema.get("why", "released_choice_missing")),
            ("probe_devices", "device_with_released_choice_rejected"),
        ]
        return self.verdict(checks, order, self.collateral())
