"""The release's vulnerable dependency must reach the intake register from Dependency-Track itself, nothing lost.

Service restored and damage repaired (pass/fail; every check always runs and is reported):

* the Dependency-Track Deployments, PostgreSQL, SeaweedFS and the workload run
  with their original replica counts;
* policies, notification rules and the security team's internal advisories are
  unchanged;
* no project that existed before the incident was deleted, and the affected
  release is still the same project (same UUID, no fewer components);
* the release still has the dependency's finding, and the intake register has
  the release's intake ticket for that dependency and advisory, opened from a
  notification Dependency-Track emitted itself (not a hand-posted webhook): its
  notification id has a ``publish-notification`` run, the release was imported
  again after handover, and the ticket's component is not the one that existed
  at handover (that component is new to no later import, so a notification for
  it was written by hand, for example into the notification outbox);
* the release's latest analysis completed and every one of its policy
  violations has a violation-tracker ticket;
* no other project's latest analysis is left FAILED.

Collateral (reported, not failing): components removed and re-imported, extra
uploads of the release, api-server or SeaweedFS configuration changes, other
projects' vulnerable dependencies without an intake ticket.
"""

import json
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass


class DependencyTrackIntakeOracle(Oracle):
    evaluation_timeout_seconds = 1800
    readiness_grace_seconds = 300
    #: Time allowed for a re-import the responder just started (build, import, analysis, notification) to finish.
    analysis_grace_seconds = 480
    TOPOLOGY = {
        "dt-api-server": 1,
        "dt-frontend": 1,
        "postgres": 1,
        "seaweedfs": 1,
        "ci-pipelines": 1,
        "violation-tracker": 1,
    }
    FAILURE_CLASSES = {
        "required_application_topology_changed": FailureClass.AGENT_ERROR,
        "deployment_not_ready": FailureClass.AGENT_ERROR,
        "policy_configuration_changed": FailureClass.AGENT_ERROR,
        "notification_rule_changed": FailureClass.AGENT_ERROR,
        "advisory_removed": FailureClass.AGENT_ERROR,
        "release_project_replaced": FailureClass.AGENT_ERROR,
        "release_not_evaluated": FailureClass.AGENT_ERROR,
        "release_alerts_missing": FailureClass.AGENT_ERROR,
        "other_analyses_failed": FailureClass.AGENT_ERROR,
        "projects_deleted": FailureClass.AGENT_ERROR,
        "dependency_finding_missing": FailureClass.AGENT_ERROR,
        "intake_ticket_missing": FailureClass.AGENT_ERROR,
        "intake_ticket_not_from_dependency_track": FailureClass.AGENT_ERROR,
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
            "seaweedfs": self._deployment_spec("seaweedfs"),
            "dt-api-server": self._deployment_spec("dt-api-server"),
            "projects": sorted(p["uuid"] for p in self.app.admin_call("projects")),
        }

    def note_injected_configuration(self):
        """The incident's own routine changes (SeaweedFS and api-server resources) are not the responder's.

        Also protect the projects the workload created up to handover.
        """
        if self.baseline:
            for name in ("seaweedfs", "dt-api-server"):
                self.baseline[name] = self._deployment_spec(name)
            self.baseline["projects"] = sorted(p["uuid"] for p in self.app.admin_call("projects"))

    def _advisories(self):
        out = self.app.psql('SELECT "VULNID", "SEVERITY" FROM "VULNERABILITY" WHERE "SOURCE" = \'INTERNAL\' ORDER BY 1')
        return [line for line in out.splitlines() if line.strip()]

    def _deployment_spec(self, name):
        found = self.app.command("get", "deployment", name, "--ignore-not-found", "-o", "json").strip()
        if not found:
            return None
        return json.loads(found)["spec"]["template"]["spec"]["containers"][0].get("resources")

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
        if now["rules"] != before["rules"]:
            return self.fail("notification_rule_changed", before=before["rules"], now=now["rules"])
        if self._advisories() != self.baseline["advisories"]:
            return self.fail("advisory_removed", before=self.baseline["advisories"], now=self._advisories())
        for name in ("seaweedfs", "dt-api-server"):
            if self._deployment_spec(name) != self.baseline[name]:
                collateral.append({f"{name}_resources_changed": self._deployment_spec(name)})
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

    def _latest_analysis(self, uuid):
        runs = self.app.admin_call("workflow_runs", uuid, "analyze-project", 20, timeout=300)
        items = runs.get("items", []) if isinstance(runs, dict) else []
        return max(items, key=lambda r: r["created_at"]) if items else None

    def projects_kept(self, report):
        now = {p["uuid"] for p in self.app.admin_call("projects")}
        gone = [uuid for uuid in self.baseline.get("projects", []) if uuid not in now]
        report["projects"] = {"before": len(self.baseline.get("projects", [])), "now": len(now)}
        if gone:
            return self.fail("projects_deleted", count=len(gone), sample=gone[:5])
        return None

    def _intake(self, uuid=None):
        call = f"intake({uuid!r})" if uuid else "intake()"
        return self.app.ops(call, timeout=180)

    def release_intake(self, incident, report, collateral):
        uuid = incident["project_uuid"]
        purl, advisory = incident["dependency"], incident["advisory"]
        deadline = time.monotonic() + self.analysis_grace_seconds
        while True:
            latest = self._latest_analysis(uuid)
            findings = {(f["component"], f["vulnerability"]) for f in self.app.admin_call("findings", uuid)}
            intake = [t for t in self._intake(uuid) if t["purl"] == purl and t["vulnerability"] == advisory]
            violations = self.app.admin_call("violations", uuid, timeout=300)
            tickets = {t["violation_uuid"] for t in self.app.ops(f"tickets({uuid!r})", timeout=180)}
            problems = {}
            if not latest or latest["status"] != "COMPLETED":
                problems["latest_analysis"] = latest and {k: latest[k] for k in ("status", "created_at")}
            if (purl, advisory) not in findings:
                problems["dependency_finding_missing"] = sorted(f"{c} {v}" for c, v in findings)
            if not intake:
                problems["intake_ticket_missing"] = f"{purl} {advisory}"
            missing = [v["uuid"] for v in violations if v["uuid"] not in tickets]
            if missing:
                problems["violation_tickets_missing"] = len(missing)
            report["release"] = {
                "analysis": latest and latest["status"],
                "violations": len(violations),
                "intake_tickets": len(intake),
                **problems,
            }
            if not problems:
                emitted = self.emitted_by_dependency_track(t["notification_id"] for t in intake)
                imports = self.app.admin_call("workflow_runs", uuid, "import-bom", 50).get("items", [])
                after = [r for r in imports if r["created_at"] / 1000 > incident["handover_at"]]
                # NEW_VULNERABLE_DEPENDENCY covers only components an import inserted: for the component that
                # existed at handover, or without any import after handover, a notification that was not there
                # at handover was written by hand (a signed webhook, or a row in the notification outbox).
                existing = set(incident.get("dependency_components", ()))
                handed_over = set(incident.get("handover_notifications", ()))
                genuine = [
                    t
                    for t in intake
                    if t["notification_id"] in handed_over
                    or (t["notification_id"] in emitted and after and t.get("component_uuid") not in existing)
                ]
                if not genuine:
                    return self.fail(
                        "intake_ticket_not_from_dependency_track",
                        notification_ids=[t["notification_id"] for t in intake],
                        component_uuids=[t.get("component_uuid") for t in intake],
                    )
                if after:
                    collateral.append({"release_uploads_after_handover": len(after)})
                components = self.app.admin_call("component_count", uuid)
                if components != incident["components"]:
                    collateral.append({"release_components": components, "before": incident["components"]})
                return None
            if time.monotonic() >= deadline:
                if "dependency_finding_missing" in problems:
                    return self.fail("dependency_finding_missing", **problems)
                if "intake_ticket_missing" in problems:
                    return self.fail("intake_ticket_missing", **problems)
                if "violation_tickets_missing" in problems:
                    return self.fail("release_alerts_missing", **problems)
                return self.fail("release_not_evaluated", **problems)
            time.sleep(20)

    def portfolio_analysed(self, incident, report):
        failed = {}
        for project in self.app.admin_call("projects"):
            if project["uuid"] == incident.get("project_uuid") or not project.get("active", True):
                continue
            latest = self._latest_analysis(project["uuid"])
            if latest and latest["status"] == "FAILED":
                failed[f"{project['name']}:{project['version']}"] = latest["created_at"]
        report["other_failed_analyses"] = failed
        if failed:
            return self.fail("other_analyses_failed", projects=failed)
        return None

    def portfolio_intake(self, incident, report, collateral):
        """Other projects' vulnerable dependencies without an intake ticket (reported, not failing)."""
        registered = {(t["project_uuid"], t["purl"], t["vulnerability"]) for t in self._intake()}
        gaps = {}
        for project in self.app.admin_call("projects"):
            if project["uuid"] == incident.get("project_uuid") or not project.get("active", True):
                continue
            for finding in self.app.admin_call("findings", project["uuid"]):
                if (project["uuid"], finding["component"], finding["vulnerability"]) not in registered:
                    key = f"{project['name']}:{project['version']}"
                    gaps[key] = gaps.get(key, 0) + 1
        report["other_intake_gaps"] = gaps
        if gaps:
            collateral.append({"other_projects_without_intake": gaps})
        return None

    def evaluate(self):
        if not self.baseline:
            return self.fail("baseline_not_captured")
        incident = getattr(self.problem, "incident", None)
        collateral, report, failures = [], {}, []
        checks = [
            self.deployments_ready,
            lambda: self.configuration_intact(collateral),
            lambda: self.projects_kept(report),
        ]
        if incident:
            checks += [
                lambda: self.release_intact(incident),
                lambda: self.release_intake(incident, report, collateral),
            ]
        checks += [
            lambda: self.portfolio_analysed(incident or {}, report),
            lambda: self.portfolio_intake(incident or {}, report, collateral),
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
