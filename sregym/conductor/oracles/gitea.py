"""Verify routing, retained Git/issue data, and PostgreSQL convergence."""

import json
import secrets
import time

from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle
from sregym.service.apps.gitea import FIXTURES


class GiteaOracle(ServiceEndpointMitigationOracle):
    evaluation_timeout_seconds = 600

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
            raise RuntimeError("The application does not have its expected persistent volumes")
        self.baseline = self.problem.app.check_workflow()
        self.verify_replication(self.baseline["token"])

    def verify_replication(self, token):
        app = self.problem.app
        cluster = app.cluster()
        status = cluster.get("status", {})
        if cluster["spec"]["instances"] != app.members or status.get("readyInstances") != app.members:
            raise RuntimeError("PostgreSQL membership or readiness changed")
        if not status.get("currentPrimary") or status.get("currentPrimary") != status.get("targetPrimary"):
            raise RuntimeError("PostgreSQL primary transition is unfinished")
        expected_sync = {"method": "any", "number": 1, "dataDurability": "required", "failoverQuorum": False}
        # CNPG's admission webhook materializes the omitted false default.
        actual_sync = {"failoverQuorum": False, **cluster["spec"].get("postgresql", {}).get("synchronous", {})}
        if app.members > 1 and actual_sync != expected_sync:
            raise RuntimeError("Required PostgreSQL synchronous replication was changed")
        pods = app.database_pods()
        if len(pods) != app.members:
            raise RuntimeError("Expected database instances are missing")
        if not all(
            any(c["type"] == "Ready" and c["status"] == "True" for c in p.get("status", {}).get("conditions", []))
            for p in pods
        ):
            raise RuntimeError("Database pods are not ready")
        primaries = []
        for pod in pods:
            name = pod["metadata"]["name"]
            if app.sql("SELECT NOT pg_is_in_recovery();", pod=name) == "t":
                primaries.append(name)
            deadline = time.monotonic() + 30
            # Tokens are generated locally as hex strings, never interpolated from agent input.
            if not token or any(c not in "0123456789abcdef" for c in token):
                raise ValueError("Invalid probe token")
            # Gitea's API title is stored in the issue table's legacy `name` column.
            while app.sql(f"SELECT count(*) FROM issue WHERE name = 'sregym-{token}';", pod=name) != "1":
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Acknowledged issue missing on database member {name}")
                time.sleep(1)
        if len(primaries) != 1:
            raise RuntimeError("Expected exactly one writable PostgreSQL primary")
        if primaries != [status["currentPrimary"]]:
            raise RuntimeError("Writable primary disagrees with the operator's current primary")

    def evaluate(self):
        if not self.baseline or self.volumes is None:
            return self.fail("baseline_not_captured")
        endpoints = super().evaluate()
        if not endpoints.get("success"):
            return endpoints
        try:
            app = self.problem.app
            deployment = json.loads(app.command("get", "deployment", "gitea", "-o", "json"))
            if deployment["spec"].get("replicas", 1) != 1:
                return self.fail("gitea_requires_one_repository_writer")
            if self.volume_ids() != self.volumes:
                return self.fail("persistent_volumes_replaced_or_missing")
            self.verify_replication(self.baseline["token"])
            app.run_client("verify", **self.baseline, fixture=json.loads((FIXTURES / "import-data.json").read_text()))
            fresh = app.check_workflow(token=secrets.token_hex(16))
            self.verify_replication(fresh["token"])
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {"success": True, "scale_tier": app.scale_tier, "database_members_verified": app.members}
