"""Require retained business records, original volumes and live PostgreSQL replicas."""

import json
import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle


class SaaSOracle(ServiceEndpointMitigationOracle):
    evaluation_timeout_seconds = 600
    FAILURE_CLASSES = {
        "persistent_volumes_replaced_or_missing": FailureClass.AGENT_ERROR,
        "required_application_topology_changed": FailureClass.AGENT_ERROR,
        "database_membership_changed": FailureClass.AGENT_ERROR,
        "database_durability_changed": FailureClass.AGENT_ERROR,
        "acknowledged_record_missing": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.baseline = None
        self.volumes = None

    def volume_ids(self):
        claims = json.loads(self.problem.app.command("get", "pvc", "-o", "json"))["items"]
        return {
            p["metadata"]["name"]: p["metadata"]["uid"] for p in claims if p.get("status", {}).get("phase") == "Bound"
        }

    def capture_baseline(self):
        self.volumes = self.volume_ids()
        if len(self.volumes) != self.problem.app.expected_volume_count:
            raise RuntimeError("Required durable volumes are not all bound")
        self.baseline = self.problem.app.check_workflow()
        outcome = self.verify_replication(self.baseline["token"])
        if outcome:
            raise RuntimeError(str(outcome))

    def verify_replication(self, token):
        if not token or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("Invalid generated probe token")
        app = self.problem.app
        cluster = app.cluster()
        status = cluster.get("status", {})
        if cluster["spec"]["instances"] != app.members:
            return self.fail("database_membership_changed")
        if app.members > 1:
            sync = cluster["spec"].get("postgresql", {}).get("synchronous", {})
            if any(sync.get(k) != v for k, v in {"method": "any", "number": 1, "dataDurability": "required"}.items()):
                return self.fail("database_durability_changed")
        if (
            status.get("readyInstances") != app.members
            or not status.get("currentPrimary")
            or status["currentPrimary"] != status.get("targetPrimary")
        ):
            raise RuntimeError("Database election or reconciliation is unfinished")
        pods = app.database_pods()
        if len(pods) != app.members:
            raise RuntimeError("Database members missing")
        primaries = []
        for p in pods:
            name = p["metadata"]["name"]
            if app.sql("SELECT NOT pg_is_in_recovery();", pod=name) == "t":
                primaries.append(name)
            deadline = time.monotonic() + 30
            while app.sql(app.record_query(token), pod=name) != "1":
                if time.monotonic() >= deadline:
                    return self.fail("acknowledged_record_missing", member=name)
                time.sleep(1)
        if primaries != [status["currentPrimary"]]:
            raise RuntimeError("Expected exactly one writable PostgreSQL primary")

    def evaluate(self):
        if not self.baseline or self.volumes is None:
            return self.fail("baseline_not_captured")
        endpoints = super().evaluate()
        if not endpoints.get("success"):
            return endpoints
        app = self.problem.app
        try:
            for name in (app.slug, *app.auxiliary_deployments):
                d = json.loads(app.command("get", "deployment", name, "-o", "json"))
                if d["spec"].get("replicas", 1) != 1:
                    return self.fail("required_application_topology_changed", deployment=name)
                if d.get("status", {}).get("readyReplicas") != 1:
                    raise RuntimeError(f"Required deployment {name} not ready")
            if self.volume_ids() != self.volumes:
                return self.fail("persistent_volumes_replaced_or_missing")
            if failed := self.verify_replication(self.baseline["token"]):
                return failed
            app.run_client("verify", **self.baseline)
            fresh = app.check_workflow()
            if failed := self.verify_replication(fresh["token"]):
                return failed
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            "success": True,
            "application": app.slug,
            "scale_tier": app.scale_tier,
            "database_members_verified": app.members,
            "original_volumes_verified": len(self.volumes),
            "business_data_verified": True,
        }
