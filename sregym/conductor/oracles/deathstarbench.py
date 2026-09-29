"""Grade the routing fault plus the scaled environment's recovery invariants."""

import json
import secrets

from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle


class ScaledServiceEndpointOracle(ServiceEndpointMitigationOracle):
    # Several database probes plus the existing endpoint oracle may exceed the default budget.
    evaluation_timeout_seconds = 600

    def __init__(self, problem):
        super().__init__(problem)
        self.deployment_oracle = MitigationOracle(problem)
        self.canary = None
        self.pvc_uids = {}

    def capture_baseline(self):
        app = self.problem.app
        self.deployment_oracle.capture_baseline()
        self.canary = secrets.token_hex(24)
        self.pvc_uids = self._pvc_snapshot()
        expected = len(app.databases) * app.scale.members
        if len(self.pvc_uids) != expected:
            raise RuntimeError(f"Expected {expected} database PVCs, found {len(self.pvc_uids)}")
        for name in app.databases:
            app.mongo_primary(
                name,
                f"""
                var r = db.getSiblingDB('sregym-durability').runCommand({{
                    insert:'canaries', documents:[{{_id:{json.dumps(self.canary)}}}],
                    writeConcern:{{w:{app.scale.members}, wtimeout:10000}}
                }});
                assert.commandWorked(r); assert(!r.writeConcernError); assert(!r.writeErrors);
            """,
            )

    def _pvc_snapshot(self):
        app = self.problem.app
        claims = json.loads(app.command("get", "pvc", "-o", "json"))["items"]
        expected = {f"data-{name}-{i}" for name in app.databases for i in range(app.scale.members)}
        return {
            p["metadata"]["name"]: p["metadata"]["uid"]
            for p in claims
            if p["metadata"]["name"] in expected and p.get("status", {}).get("phase") == "Bound"
        }

    def evaluate(self):
        if self.canary is None:
            return self.fail("baseline_not_captured")
        result = super().evaluate()
        if not result.get("success"):
            return result
        result = self.deployment_oracle.evaluate()
        if not result.get("success"):
            return result
        app = self.problem.app
        try:
            if self._pvc_snapshot() != self.pvc_uids:
                return self.fail("persistent_volumes_replaced_or_missing")
            stateful_sets = {
                item["metadata"]["name"]: item
                for item in json.loads(app.command("get", "statefulsets", "-o", "json"))["items"]
            }
            for name in app.databases:
                stateful = stateful_sets.get(name, {})
                status = stateful.get("status", {})
                if (
                    stateful.get("spec", {}).get("replicas") != app.scale.members
                    or status.get("readyReplicas", 0) != app.scale.members
                    or status.get("observedGeneration", 0) < stateful.get("metadata", {}).get("generation", 1)
                    or status.get("currentRevision") != status.get("updateRevision")
                ):
                    return self.fail("database_members_not_ready", database=name)
            for name in app.databases:
                # Verify the live set, not just Ready containers. Refuse forced one-member reconfiguration.
                app.mongo(
                    name,
                    f"""
                    var s=rs.status(); assert.commandWorked(s);
                    assert.eq({app.scale.members}, s.members.length);
                    assert.eq(1,s.members.filter(function(m) {{ return m.state===1 && m.health===1; }}).length);
                    assert(s.members.every(function(m) {{ return m.health===1 && (m.state===1 || m.state===2); }}));
                """,
                )
                token = secrets.token_hex(16)
                # All-member acknowledgement forces a recovery tail: serving traffic alone is insufficient.
                app.mongo_primary(
                    name,
                    f"""
                    var d=db.getSiblingDB('sregym-durability');
                    var r=d.runCommand({{update:'progress', updates:[{{q:{{_id:'latest'}},
                        u:{{$set:{{token:{json.dumps(token)}}}}}, upsert:true}}],
                        writeConcern:{{w:{app.scale.members}, wtimeout:10000}}}});
                    assert.commandWorked(r); assert(!r.writeConcernError); assert(!r.writeErrors);
                """,
                )
                for member in range(app.scale.members):
                    app.mongo(
                        name,
                        f"""
                        rs.slaveOk(); var d=db.getSiblingDB('sregym-durability');
                        assert(d.canaries.findOne({{_id:{json.dumps(self.canary)}}}));
                        assert.eq({json.dumps(token)},d.progress.findOne({{_id:'latest'}}).token);
                    """,
                        member=member,
                    )
            app.check_workflow()
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            "success": True,
            "scale_tier": app.scale_tier,
            "database_members_verified": len(app.databases) * app.scale.members,
        }
