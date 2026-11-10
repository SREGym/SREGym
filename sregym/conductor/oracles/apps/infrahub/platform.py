"""Shared Infrahub invariants: intact topology, original volumes and every pre-incident object.

Problem-specific oracles add their user-visible checks on top. A repair that
scales a component away, replaces the Neo4j volume or drops inventory fails here
whatever else it fixed.
"""

import json
import logging
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

logger = logging.getLogger("all.sregym.oracles.infrahub")


class InfrahubPlatformOracle(Oracle):
    evaluation_timeout_seconds = 1200
    #: Bounded wait for a deployment the responder just restarted to become ready again.
    readiness_grace_seconds = 300
    FAILURE_CLASSES = {
        "required_application_topology_changed": FailureClass.AGENT_ERROR,
        "deployment_not_ready": FailureClass.AGENT_ERROR,
        "persistent_volumes_replaced_or_missing": FailureClass.AGENT_ERROR,
        "pre_incident_objects_missing": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.baseline = None

    @property
    def app(self):
        return self.problem.app

    def capture_baseline(self):
        app = self.app
        inventory = app.run_client("inventory", exec_timeout=300)
        self.baseline = {
            "volumes": app.volume_ids(),
            "devices": inventory["names"],
            "modules": inventory["modules"],
            "device_count": inventory["count"],
        }
        if not self.baseline["devices"]:
            raise RuntimeError("No inventory was seeded before the incident")

    def deployments_ready(self):
        """Return a failure verdict, or None once every topology Deployment is complete."""
        app = self.app
        deadline = time.monotonic() + self.readiness_grace_seconds
        while True:
            pending = None
            for name, replicas in app.topology.items():
                found = app.command("get", "deployment", name, "--ignore-not-found", "-o", "json").strip()
                if not found:
                    return self.fail("required_application_topology_changed", deployment=name, missing=True)
                deployment = json.loads(found)
                spec = deployment["spec"].get("replicas", 1)
                if spec != replicas:
                    return self.fail(
                        "required_application_topology_changed", deployment=name, replicas=spec, expected=replicas
                    )
                status = deployment.get("status", {})
                if (
                    status.get("observedGeneration", 0) < deployment["metadata"]["generation"]
                    or status.get("updatedReplicas", 0) != replicas
                    or status.get("readyReplicas", 0) != replicas
                    or status.get("replicas", 0) != replicas
                ):
                    pending = name
                    break
            if pending is None:
                return None
            if time.monotonic() >= deadline:
                return self.fail("deployment_not_ready", deployment=pending)
            time.sleep(5)

    def platform_intact(self):
        if failed := self.deployments_ready():
            return failed
        app = self.app
        volumes = app.volume_ids()
        changed = [n for n, uid in self.baseline["volumes"].items() if volumes.get(n) != uid]
        if changed:
            return self.fail("persistent_volumes_replaced_or_missing", volumes=changed)
        for kind, names in (("NetworkDevice", self.baseline["devices"]), ("NetworkModule", self.baseline["modules"])):
            missing = app.run_client("missing_devices", names=names, kind=kind, exec_timeout=300)["missing"]
            if missing:
                return self.fail("pre_incident_objects_missing", kind=kind, count=len(missing), sample=missing[:5])
        return None

    # ---------------------------------------------------------------- reporting checks
    # Oracles built on these run every check, report each one in ``checks`` and the pod
    # restarts since handover in ``collateral``; the verdict's reason is the first failed
    # check in the oracle's documented order.

    def snapshot_pods(self):
        """{app label: {pod uid: restart count}} for every topology Deployment."""
        snapshot = {}
        for name in self.app.topology:
            snapshot[name] = {
                p["metadata"]["uid"]: sum(
                    c.get("restartCount", 0) for c in p.get("status", {}).get("containerStatuses", [])
                )
                for p in self.app.pods(name)
                if not p["metadata"].get("deletionTimestamp")
            }
        return snapshot

    def capture_handover(self):
        """Record the pods the responder is handed (collateral is reported against them)."""
        self.handover_pods = self.snapshot_pods()

    def collateral(self):
        before = getattr(self, "handover_pods", None)
        if not before:
            return {"restarted": None}
        now = self.snapshot_pods()
        restarted = sorted(
            name
            for name, pods in now.items()
            if set(pods) != set(before.get(name, {})) or any(n > before[name].get(uid, 0) for uid, n in pods.items())
        )
        return {"restarted": restarted}

    @staticmethod
    def run_check(checks, name, fn):
        try:
            checks[name] = fn()
        except Exception as exc:  # a probe error is reported, not raised
            logger.exception("Infrahub oracle check %s raised", name)
            checks[name] = {"ok": None, "error": f"{type(exc).__name__}: {str(exc)[-500:]}"}

    def verdict(self, checks, order, collateral, **extra):
        for name, reason in order:
            if checks.get(name, {}).get("ok") is False:
                verdict = self.fail(reason, check=name)
                break
        else:
            unknown = [name for name, _ in order if checks.get(name, {}).get("ok") is None]
            verdict = self.fail("probe_failed", checks=unknown) if unknown else {"success": True}
        verdict.update(extra, checks=checks, collateral=collateral)
        print(
            f"[infrahub oracle] success={verdict.get('success')} reason={verdict.get('reason')} "
            f"collateral={json.dumps(collateral, default=str)}"
        )
        for name, result in checks.items():
            print(f"[infrahub oracle]   {name}: {json.dumps(result, default=str)[:800]}")
        return verdict

    def topology_check(self):
        failed = self.deployments_ready()
        return {"ok": True} if failed is None else {"ok": False, **failed.get("detail", {}), "why": failed["reason"]}

    def volumes_check(self):
        volumes = self.app.volume_ids()
        changed = [n for n, uid in self.baseline["volumes"].items() if volumes.get(n) != uid]
        return {"ok": not changed, "replaced": changed}

    def pre_incident_check(self):
        missing = {}
        for kind, names in (("NetworkDevice", self.baseline["devices"]), ("NetworkModule", self.baseline["modules"])):
            found = self.app.run_client("missing_devices", names=names, kind=kind, exec_timeout=600)["missing"]
            if found:
                missing[kind] = {"count": len(found), "sample": found[:5]}
        return {"ok": not missing, "missing": missing, "verified": len(self.baseline["devices"])}

    def platform_checks(self, checks):
        self.run_check(checks, "topology", self.topology_check)
        self.run_check(checks, "volumes", self.volumes_check)
        self.run_check(checks, "pre_incident_objects", self.pre_incident_check)

    PLATFORM_ORDER = [
        ("topology", "required_application_topology_changed"),
        ("volumes", "persistent_volumes_replaced_or_missing"),
        ("pre_incident_objects", "pre_incident_objects_missing"),
    ]

    def evaluate(self):
        if not self.baseline:
            return self.fail("baseline_not_captured")
        try:
            if failed := self.platform_intact():
                return failed
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {"success": True, "pre_incident_devices_verified": len(self.baseline["devices"])}
