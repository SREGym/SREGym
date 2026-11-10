"""Every vulnerable dependency the release introduced must reach the review queue from Dependency-Track itself.

Service restored and damage repaired (pass/fail; every check always runs and is reported):

* the Dependency-Track Deployments, PostgreSQL, SeaweedFS and the workload run
  with their original replica counts;
* policies are unchanged, the internal advisories are all still there, and the
  ``dependency-review`` and ``security-alerts`` rules are still enabled, still
  subscribe their groups and still point at their receivers;
* the affected release still exists as the same project (same UUID, no fewer
  components) and still has unsuppressed findings for its vulnerable
  dependencies;
* every current finding of the release (component, vulnerability) has a
  review item in ``dependency-review`` that came from a NEW_VULNERABLE_DEPENDENCY
  notification Dependency-Track emitted itself (its notification id has a
  ``publish-notification`` run, and its component was imported as new: a
  component that already existed at handover is new to no later import, so a
  later notification for it was written by hand, e.g. into the notification
  outbox), and every policy violation of the release has a violation-tracker
  ticket.

Collateral (reported, not failing): components of the release deleted and
re-created (new component UUIDs, superseded tickets in the violation-tracker),
re-uploads of the release, review items posted by hand, notification rule
changes that keep both rules working, and api-server configuration changes.
"""

import json
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

RULE_GROUPS = {"dependency-review": "NEW_VULNERABLE_DEPENDENCY", "security-alerts": "POLICY_VIOLATION"}


class DependencyTrackDependencyReviewOracle(Oracle):
    evaluation_timeout_seconds = 1800
    readiness_grace_seconds = 300
    #: Time allowed for an import or analysis the responder just started to finish and its alerts to arrive.
    analysis_grace_seconds = 900
    TOPOLOGY = {
        "dt-api-server": 1,
        "dt-frontend": 1,
        "postgres": 1,
        "seaweedfs": 1,
        "ci-pipelines": 1,
        "violation-tracker": 1,
        "dependency-review": 1,
    }
    FAILURE_CLASSES = {
        "required_application_topology_changed": FailureClass.AGENT_ERROR,
        "deployment_not_ready": FailureClass.AGENT_ERROR,
        "policy_configuration_changed": FailureClass.AGENT_ERROR,
        "notification_rule_broken": FailureClass.AGENT_ERROR,
        "advisory_removed": FailureClass.AGENT_ERROR,
        "release_project_replaced": FailureClass.AGENT_ERROR,
        "release_findings_removed": FailureClass.AGENT_ERROR,
        "release_reviews_missing": FailureClass.AGENT_ERROR,
        "reviews_not_from_dependency_track": FailureClass.AGENT_ERROR,
        "release_tickets_missing": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.baseline = None

    @property
    def app(self):
        return self.problem.app

    def capture_baseline(self):
        self.baseline = {
            "configuration": self.app.admin_call("configuration"),
            "advisories": self._advisories(),
            "dt-api-server": self._deployment_spec("dt-api-server"),
        }

    def note_injected_configuration(self):
        """The incident's own routine api-server changes are not the responder's."""
        if self.baseline:
            self.baseline["dt-api-server"] = self._deployment_spec("dt-api-server")

    def _advisories(self):
        out = self.app.psql('SELECT "VULNID", "SEVERITY" FROM "VULNERABILITY" WHERE "SOURCE" = \'INTERNAL\' ORDER BY 1')
        return [line for line in out.splitlines() if line.strip()]

    def _deployment_spec(self, name):
        found = self.app.command("get", "deployment", name, "--ignore-not-found", "-o", "json").strip()
        if not found:
            return None
        container = json.loads(found)["spec"]["template"]["spec"]["containers"][0]
        return {"resources": container.get("resources"), "env": container.get("env")}

    # ------------------------------------------------------------------ checks
    def deployments_ready(self):
        deadline = time.monotonic() + self.readiness_grace_seconds
        while True:
            pending = None
            for name, replicas in self.TOPOLOGY.items():
                found = self.app.command("get", "deployment", name, "--ignore-not-found", "-o", "json").strip()
                if not found:
                    return self.fail("required_application_topology_changed", deployment=name, missing=True)
                deployment = json.loads(found)
                spec = deployment["spec"].get("replicas", 1)
                if spec != replicas:
                    return self.fail("required_application_topology_changed", deployment=name, replicas=spec)
                status = deployment.get("status", {})
                if (
                    status.get("observedGeneration", 0) < deployment["metadata"]["generation"]
                    or status.get("updatedReplicas", 0) != replicas
                    or status.get("readyReplicas", 0) != replicas
                ):
                    pending = name
                    break
            if pending is None:
                return None
            if time.monotonic() >= deadline:
                return self.fail("deployment_not_ready", deployment=pending)
            time.sleep(5)

    def configuration_intact(self, collateral):
        now = self.app.admin_call("configuration")
        before = self.baseline["configuration"]
        if now["policies"] != before["policies"]:
            return self.fail("policy_configuration_changed", before=before["policies"], now=now["policies"])
        missing = sorted(set(self.baseline["advisories"]) - set(self._advisories()))
        if missing:
            return self.fail("advisory_removed", missing=missing)
        if now["rules"] != before["rules"]:
            collateral.append({"notification_rules_changed": now["rules"]})
        rules = {r["name"]: r for r in now["rules"]}
        original = {r["name"]: r for r in before["rules"]}
        for name, group in RULE_GROUPS.items():
            rule, was = rules.get(name), original.get(name)
            if was is None:
                continue
            if (
                not rule
                or not rule["enabled"]
                or group not in rule["notifyOn"]
                or rule["destination"] != was["destination"]
                or rule["scope"] != was["scope"]
                or rule["projects"] != was["projects"]
                or rule["tags"] != was["tags"]
                or rule["filter"] != was["filter"]
            ):
                return self.fail("notification_rule_broken", rule=name, now=rule)
        if self._deployment_spec("dt-api-server") != self.baseline["dt-api-server"]:
            collateral.append({"dt-api-server_configuration_changed": self._deployment_spec("dt-api-server")})
        return None

    def release_intact(self, incident):
        uuid = incident["project_uuid"]
        if not self.app.admin_call("project_exists", uuid):
            return self.fail("release_project_replaced", project_uuid=uuid)
        components = self.app.admin_call("component_count", uuid)
        if components < incident["components"]:
            return self.fail("release_project_replaced", components=components, expected=incident["components"])
        return None

    def emitted_by_dependency_track(self, notification_ids):
        found = set()
        ids = sorted({i for i in notification_ids if i})
        for start in range(0, len(ids), 500):
            chunk = ",".join(f"'publish-notification:{i}'" for i in ids[start : start + 500])
            out = self.app.psql(
                f"SELECT workflow_instance_id FROM dex_workflow_run WHERE workflow_instance_id IN ({chunk})"
            )
            found |= {line.strip().removeprefix("publish-notification:") for line in out.splitlines() if line.strip()}
        return found

    def release_reviewed(self, incident, report, collateral):
        uuid = incident["project_uuid"]
        existing = set(incident["finding_components"])
        handed_over = set(incident.get("handover_notifications", ()))
        deadline = time.monotonic() + self.analysis_grace_seconds
        while True:
            findings = self.app.admin_call("findings", uuid, timeout=300)
            reviews = self.app.ops(f"reviews({uuid!r})", timeout=120)
            violations = self.app.admin_call("violations", uuid, timeout=300)
            tickets = {t["violation_uuid"] for t in self.app.ops(f"tickets({uuid!r})", timeout=180)}
            emitted = self.emitted_by_dependency_track(r["notification_id"] for r in reviews)
            # NEW_VULNERABLE_DEPENDENCY covers only components an import inserted. For a component that
            # already existed at handover, a notification that was not there at handover was forged.
            genuine = {
                (r["component_uuid"], r["vulnerability"])
                for r in reviews
                if r["notification_id"] in emitted
                and (r["component_uuid"] not in existing or r["notification_id"] in handed_over)
            }
            posted = {(r["component_uuid"], r["vulnerability"]) for r in reviews} - genuine
            current = [f for f in findings if not f["suppressed"]]
            suppressed = sorted({f["vulnerability"] for f in findings if f["suppressed"]})
            advisories = {f["vulnerability"] for f in current}
            lost = sorted(set(incident["advisories"]) - advisories)
            unreviewed = [f for f in current if (f["component_uuid"], f["vulnerability"]) not in genuine]
            untracked = [v["uuid"] for v in violations if v["uuid"] not in tickets]
            report["release"] = {
                "findings": len(current),
                "suppressed": suppressed,
                "reviews": len(reviews),
                "reviews_from_dependency_track": len(genuine),
                "unreviewed": [f"{f['component']} {f['vulnerability']}" for f in unreviewed],
                "violations": len(violations),
                "tickets_missing": len(untracked),
            }
            settled = not lost and not suppressed and not unreviewed and not untracked
            if settled or time.monotonic() >= deadline:
                break
            time.sleep(20)
        components = {f["component_uuid"] for f in findings}
        if components - set(incident["finding_components"]):
            collateral.append({"release_components_recreated": len(components - set(incident["finding_components"]))})
        if posted:
            collateral.append({"review_items_not_from_dependency_track": len(posted)})
        if lost or suppressed:
            return self.fail("release_findings_removed", missing=lost, suppressed=suppressed)
        if unreviewed:
            if posted & {(f["component_uuid"], f["vulnerability"]) for f in unreviewed}:
                return self.fail("reviews_not_from_dependency_track", unreviewed=report["release"]["unreviewed"])
            return self.fail("release_reviews_missing", unreviewed=report["release"]["unreviewed"])
        if untracked:
            return self.fail("release_tickets_missing", missing=len(untracked), sample=untracked[:5])
        return None

    def release_reuploads(self, incident, collateral):
        runs = self.app.admin_call("workflow_runs", incident["project_uuid"], "import-bom", 50, timeout=300)
        items = runs.get("items", []) if isinstance(runs, dict) else []
        later = [r for r in items if r["created_at"] / 1000 > incident["handover_ready_at"]]
        if later:
            collateral.append({"release_reuploads": len(later)})

    def evaluate(self):
        if not self.baseline:
            return self.fail("baseline_not_captured")
        incident = getattr(self.problem, "incident", None)
        collateral, report, failures = [], {}, []
        checks = [self.deployments_ready, lambda: self.configuration_intact(collateral)]
        if incident:
            checks += [
                lambda: self.release_intact(incident),
                lambda: self.release_reviewed(incident, report, collateral),
                lambda: self.release_reuploads(incident, collateral),
            ]
        for check in checks:
            try:
                failed = check()
            except Exception as exc:
                failed = self.fail_from_exception(exc)
            if failed:
                failures.append(failed)
        if failures:
            result = dict(failures[0])
            result.update(all_failures=failures, collateral=collateral, report=report)
            return result
        return {"success": True, "collateral": collateral, "report": report}
