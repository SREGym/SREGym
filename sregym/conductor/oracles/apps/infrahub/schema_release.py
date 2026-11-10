"""Every released attribute must be served by every API worker and be usable, with the release still on.

Many fresh-connection schema reads spread over the four gunicorn workers, so a
worker that still holds a stale in-memory schema registry fails the check even
when the stored schema is right. Creating and reading devices and modules that
set every released attribute is what inventory-sync needs. Taking an attribute
out of inventory-sync's configuration (turning the release off) fails, as do
the platform invariants (topology, volumes, pre-incident objects).
"""

from sregym.conductor.oracles.apps.infrahub.platform import InfrahubPlatformOracle
from sregym.conductor.oracles.failure import FailureClass


class InfrahubSchemaReleaseOracle(InfrahubPlatformOracle):
    FAILURE_CLASSES = {
        "released_attribute_missing": FailureClass.AGENT_ERROR,
        "api_workers_disagree_on_schema": FailureClass.AGENT_ERROR,
        "object_with_released_attributes_rejected": FailureClass.AGENT_ERROR,
        "release_turned_off": FailureClass.AGENT_ERROR,
    }
    schema_samples = 32
    rounds = 2

    def __init__(self, problem, base_fields, released):
        super().__init__(problem)
        self.base_fields = {kind: list(fields) for kind, fields in base_fields.items()}
        self.release = {kind: list(fields) for kind, fields in released.items()}

    @property
    def fields(self):
        """Attributes inventory-sync must be able to set per kind (the released ones once the release ran)."""
        fields = {kind: list(f) for kind, f in self.base_fields.items()}
        if getattr(self.problem, "releases_started", False):
            for kind, extra in self.release.items():
                fields[kind] += extra
        return fields

    def evaluate(self):
        if not self.baseline:
            return self.fail("baseline_not_captured")
        app = self.app
        fields = self.fields
        try:
            if failed := self.platform_intact():
                return failed
            configured = app.configured_inventory_fields() or {}
            dropped = {k: sorted(set(f) - set(configured.get(k, []))) for k, f in fields.items()}
            dropped = {k: v for k, v in dropped.items() if v}
            if dropped:
                return self.fail("release_turned_off", removed_from_inventory_sync=dropped)
            for round_number in range(self.rounds):
                views = app.run_client("views", kinds=list(fields), samples=self.schema_samples, exec_timeout=600)
                for kind, served in views.items():
                    if len(served) > 1:
                        return self.fail("api_workers_disagree_on_schema", kind=kind, views=served, round=round_number)
                    missing = sorted(set(fields[kind]) - set(next(iter(served)).split(",")))
                    if missing:
                        return self.fail("released_attribute_missing", kind=kind, missing=missing, views=served)
                probe = app.run_client(
                    "probe_objects",
                    device_fields=fields["NetworkDevice"],
                    module_fields=fields["NetworkModule"],
                    count=4,
                    exec_timeout=300,
                )
                if not probe["ok"]:
                    bad = [r for r in probe["results"] if not r["ok"]]
                    return self.fail("object_with_released_attributes_rejected", sample=bad[:2])
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            "success": True,
            "fields": fields,
            "schema_reads_verified": self.schema_samples * self.rounds * len(fields),
            "pre_incident_devices_verified": len(self.baseline["devices"]),
        }
