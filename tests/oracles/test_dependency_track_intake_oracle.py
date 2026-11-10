"""Verdicts of the dependency intake oracle (Dependency-Track) against a scripted application."""

import copy
import json
from types import SimpleNamespace

from sregym.conductor.oracles.apps.dependency_track.dependency_intake import DependencyTrackIntakeOracle

RELEASE = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
DEPENDENCY = "pkg:npm/moment@7.9.19"
ADVISORY = "INT-2026-0114"
READY = json.dumps(
    {
        "metadata": {"generation": 1},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"resources": {"limits": {"memory": "2Gi"}}}]}}},
        "status": {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1},
    }
)
CONFIG = {
    "policies": [{"name": "Vulnerabilities rated high or critical", "conditions": [["SEVERITY", "IS", "HIGH"]]}],
    "rules": [{"name": "security-alerts", "enabled": True, "notifyOn": ["NEW_VULNERABLE_DEPENDENCY"]}],
}


class IntakeApp:
    def __init__(self):
        self.latest = {"status": "COMPLETED", "created_at": 2_000_000}
        self.violations = [{"uuid": "v1", "policy": "Vulnerabilities rated high or critical"}]
        self.tickets = [{"violation_uuid": "v1", "notification_id": "n-v1"}]
        self.intake = [
            {
                "project_uuid": RELEASE,
                "component_uuid": "c-new",
                "purl": DEPENDENCY,
                "vulnerability": ADVISORY,
                "notification_id": "n-dep",
            },
            {"project_uuid": OTHER, "purl": "pkg:npm/micromatch@6.27.3", "vulnerability": "INT-2026-0107"},
        ]
        self.emitted = {"n-v1", "n-dep"}
        self.findings = {
            RELEASE: [{"component": DEPENDENCY, "vulnerability": ADVISORY}],
            OTHER: [{"component": "pkg:npm/micromatch@6.27.3", "vulnerability": "INT-2026-0107"}],
        }
        self.projects = [
            {"uuid": RELEASE, "name": "checkout-web", "version": "5.13.0", "active": True},
            {"uuid": OTHER, "name": "mobile-bff", "version": "4.4.0", "active": True},
        ]
        self.advisories = "INT-2026-0107|HIGH\nINT-2026-0114|HIGH"
        self.config = copy.deepcopy(CONFIG)
        self.other_status = "COMPLETED"
        self.components = 221
        self.imports = [
            {"status": "COMPLETED", "created_at": 1_000_000},
            {"status": "COMPLETED", "created_at": 1_800_000},
        ]

    def command(self, *args, **kwargs):
        return READY

    def admin_call(self, name, *args, **kwargs):
        if name == "configuration":
            return copy.deepcopy(self.config)
        if name == "projects":
            return copy.deepcopy(self.projects)
        if name == "project_exists":
            return any(p["uuid"] == args[0] for p in self.projects)
        if name == "component_count":
            return self.components
        if name == "workflow_runs":
            if args[0] == RELEASE and args[1] == "import-bom":
                return {"items": self.imports}
            if args[0] == RELEASE:
                return {"items": [{"status": "FAILED", "created_at": 1_000_000}, self.latest]}
            return {"items": [{"status": self.other_status, "created_at": 1_500_000}]}
        if name == "findings":
            return self.findings.get(args[0], [])
        if name == "violations":
            return self.violations
        raise AssertionError(name)

    def ops(self, call, **kwargs):
        if call.startswith("tickets("):
            return self.tickets
        if call.startswith("intake("):
            if call == "intake()":
                return self.intake
            return [t for t in self.intake if t["project_uuid"] == RELEASE]
        raise AssertionError(call)

    def psql(self, sql, **kwargs):
        if '"VULNERABILITY"' in sql:
            return self.advisories
        return "\n".join(f"publish-notification:{n}" for n in self.emitted if f"'publish-notification:{n}'" in sql)


def intake_oracle(app, handover_notifications=()):
    problem = SimpleNamespace(
        app=app,
        incident={
            "project_uuid": RELEASE,
            "components": 221,
            "dependency": DEPENDENCY,
            "advisory": ADVISORY,
            "dependency_components": ["c-old"],
            "handover_notifications": list(handover_notifications),
            "handover_at": 1500,
        },
    )
    checker = DependencyTrackIntakeOracle(problem)
    checker.baseline = {
        "configuration": copy.deepcopy(CONFIG),
        "advisories": app.advisories.splitlines(),
        "seaweedfs": {"limits": {"memory": "2Gi"}},
        "dt-api-server": {"limits": {"memory": "2Gi"}},
        "projects": sorted(p["uuid"] for p in app.projects),
    }
    checker.readiness_grace_seconds = checker.analysis_grace_seconds = 0
    return checker


def test_intake_passes_after_the_dependency_was_imported_again():
    assert intake_oracle(IntakeApp()).evaluate()["success"] is True


def test_intake_fails_without_the_intake_ticket():
    app = IntakeApp()
    app.intake = app.intake[1:]
    verdict = intake_oracle(app).evaluate()
    assert verdict["reason"] == "intake_ticket_missing"


def test_intake_rejects_a_hand_posted_ticket():
    app = IntakeApp()
    app.emitted = {"n-v1"}
    assert intake_oracle(app).evaluate()["reason"] == "intake_ticket_not_from_dependency_track"


def test_intake_rejects_a_notification_written_into_the_outbox():
    # Dependency-Track publishes an outbox row (a publish-notification run exists), but it never emits
    # NEW_VULNERABLE_DEPENDENCY for the component that already existed at handover.
    app = IntakeApp()
    app.intake[0]["component_uuid"] = "c-old"
    assert intake_oracle(app).evaluate()["reason"] == "intake_ticket_not_from_dependency_track"


def test_intake_rejects_an_emitted_notification_without_a_reimport():
    app = IntakeApp()
    app.imports = app.imports[:1]
    assert intake_oracle(app).evaluate()["reason"] == "intake_ticket_not_from_dependency_track"


def test_intake_ticket_present_at_handover_passes():
    app = IntakeApp()
    app.intake[0]["component_uuid"] = "c-old"
    app.imports = app.imports[:1]
    assert intake_oracle(app, handover_notifications=["n-dep"]).evaluate()["success"] is True


def test_intake_rejects_a_deleted_release_or_finding():
    app = IntakeApp()
    checker = intake_oracle(app)
    app.projects = app.projects[1:]
    verdict = checker.evaluate()
    assert verdict["reason"] == "projects_deleted"
    assert "release_project_replaced" in {f["reason"] for f in verdict["all_failures"]}
    app = IntakeApp()
    app.findings[RELEASE] = []
    assert intake_oracle(app).evaluate()["reason"] == "dependency_finding_missing"


def test_intake_rejects_changed_advisories_and_missing_violation_tickets():
    app = IntakeApp()
    checker = intake_oracle(app)
    app.advisories = "INT-2026-0107|HIGH"
    assert checker.evaluate()["reason"] == "advisory_removed"
    app = IntakeApp()
    app.tickets = []
    assert intake_oracle(app).evaluate()["reason"] == "release_alerts_missing"


def test_intake_reports_other_gaps_as_collateral_and_fails_other_failed_analyses():
    app = IntakeApp()
    app.intake = app.intake[:1]
    verdict = intake_oracle(app).evaluate()
    assert verdict["success"] is True
    assert {"other_projects_without_intake": {"mobile-bff:4.4.0": 1}} in verdict["collateral"]
    app = IntakeApp()
    app.other_status = "FAILED"
    assert intake_oracle(app).evaluate()["reason"] == "other_analyses_failed"


def test_healthy_deployment_passes_without_an_incident():
    app = IntakeApp()
    checker = intake_oracle(app)
    checker.problem.incident = None
    assert checker.evaluate()["success"] is True
