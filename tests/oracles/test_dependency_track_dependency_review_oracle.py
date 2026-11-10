"""Verdicts of the dependency review oracle (Dependency-Track) against a scripted application."""

import copy
import json
from types import SimpleNamespace

from sregym.conductor.oracles.apps.dependency_track.dependency_reviews import DependencyTrackDependencyReviewOracle

RELEASE = "11111111-1111-1111-1111-111111111111"
READY = json.dumps(
    {
        "metadata": {"generation": 1},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"resources": {"limits": {"memory": "2Gi"}}}]}}},
        "status": {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1},
    }
)
RULE = {"scope": "PORTFOLIO", "projects": 0, "tags": 0, "filter": None, "enabled": True}
CONFIG = {
    "policies": [{"name": "Vulnerabilities rated high or critical", "conditions": [["SEVERITY", "IS", "HIGH"]]}],
    "rules": [
        {**RULE, "name": "dependency-review", "notifyOn": ["NEW_VULNERABLE_DEPENDENCY"], "destination": "http://r"},
        {**RULE, "name": "security-alerts", "notifyOn": ["POLICY_VIOLATION"], "destination": "http://t"},
    ],
}
FINDINGS = [
    {"component": "pkg:npm/micromatch@6.27.19", "component_uuid": "c1", "vulnerability": "INT-2026-0107"},
    {"component": "pkg:npm/axios@3.1.2", "component_uuid": "c2", "vulnerability": "INT-2026-0071"},
]


class ReviewApp:
    def __init__(self):
        self.findings = [{**f, "suppressed": False} for f in FINDINGS]
        self.reviews = []
        self.emitted = set()
        self.violations = [{"uuid": "v1"}]
        self.tickets = [{"violation_uuid": "v1"}]
        self.config = copy.deepcopy(CONFIG)
        self.advisories = "INT-2026-0071|MEDIUM\nINT-2026-0107|HIGH"
        self.components = 230
        self.exists = True

    def review(self, component, vulnerability, notification, emitted=True):
        self.reviews.append(
            {"component_uuid": component, "vulnerability": vulnerability, "notification_id": notification}
        )
        if emitted:
            self.emitted.add(notification)

    def command(self, *args, **kwargs):
        return READY

    def admin_call(self, name, *args, **kwargs):
        if name == "configuration":
            return copy.deepcopy(self.config)
        if name == "project_exists":
            return self.exists
        if name == "component_count":
            return self.components
        if name == "findings":
            return copy.deepcopy(self.findings)
        if name == "violations":
            return self.violations
        if name == "workflow_runs":
            return {"items": [{"created_at": 1_000_000}]}
        raise AssertionError(name)

    def ops(self, call, timeout=None):
        if call.startswith("reviews("):
            return self.reviews
        if call.startswith("tickets("):
            return self.tickets
        raise AssertionError(call)

    def psql(self, sql, timeout=None):
        if '"VULNERABILITY"' in sql:
            return self.advisories
        ids = [i.strip("'").removeprefix("publish-notification:") for i in sql.split("IN (")[1].rstrip(")").split(",")]
        return "\n".join(f"publish-notification:{i}" for i in ids if i in self.emitted)


def recreate(app):
    app.findings = [{**f, "component_uuid": f["component_uuid"] + "-new", "suppressed": False} for f in FINDINGS]


def oracle_for(app, handover_notifications=()):
    incident = {
        "project_uuid": RELEASE,
        "components": 230,
        "advisories": ["INT-2026-0071", "INT-2026-0107"],
        "finding_components": ["c1", "c2"],
        "handover_ready_at": 2_000,
        "handover_notifications": list(handover_notifications),
    }
    oracle = DependencyTrackDependencyReviewOracle(SimpleNamespace(app=app, incident=incident))
    oracle.readiness_grace_seconds = 0
    oracle.analysis_grace_seconds = 0
    oracle.capture_baseline()
    return oracle


def test_lost_reviews_fail():
    result = oracle_for(ReviewApp()).evaluate()
    assert result["reason"] == "release_reviews_missing"
    assert len(result["report"]["release"]["unreviewed"]) == 2


def test_recreated_components_with_emitted_reviews_pass():
    app = ReviewApp()
    oracle = oracle_for(app)
    recreate(app)
    app.review("c1-new", "INT-2026-0107", "n1")
    app.review("c2-new", "INT-2026-0071", "n2")
    result = oracle.evaluate()
    assert result["success"] is True
    assert {"release_components_recreated": 2} in result["collateral"]


def test_hand_posted_reviews_fail():
    app = ReviewApp()
    app.review("c1", "INT-2026-0107", "forged-1", emitted=False)
    app.review("c2", "INT-2026-0071", "forged-2", emitted=False)
    assert oracle_for(app).evaluate()["reason"] == "reviews_not_from_dependency_track"


def test_notifications_written_into_the_outbox_fail():
    # Dependency-Track publishes them (publish-notification runs exist), but it cannot have emitted
    # NEW_VULNERABLE_DEPENDENCY for components that already existed at handover.
    app = ReviewApp()
    oracle = oracle_for(app)
    app.review("c1", "INT-2026-0107", "outbox-1")
    app.review("c2", "INT-2026-0071", "outbox-2")
    assert oracle.evaluate()["reason"] == "reviews_not_from_dependency_track"


def test_reviews_present_at_handover_pass():
    app = ReviewApp()
    app.review("c1", "INT-2026-0107", "n1")
    app.review("c2", "INT-2026-0071", "n2")
    assert oracle_for(app, handover_notifications=["n1", "n2"]).evaluate()["success"] is True


def test_suppressed_or_removed_findings_fail():
    app = ReviewApp()
    oracle = oracle_for(app)
    app.findings[1]["suppressed"] = True
    app.review("c1", "INT-2026-0107", "n1")
    assert oracle.evaluate()["reason"] == "release_findings_removed"
    app.findings = app.findings[:1]
    assert oracle.evaluate()["reason"] == "release_findings_removed"


def test_deleted_project_and_broken_rule_fail():
    app = ReviewApp()
    oracle = oracle_for(app)
    app.exists = False
    assert oracle.evaluate()["reason"] == "release_project_replaced"
    app.exists = True
    app.config["rules"][0]["enabled"] = False
    assert oracle.evaluate()["reason"] == "notification_rule_broken"


def test_harmless_rule_change_is_collateral():
    app = ReviewApp()
    oracle = oracle_for(app)
    app.config["rules"][0]["notifyOn"] = ["NEW_VULNERABILITY", "NEW_VULNERABLE_DEPENDENCY"]
    recreate(app)
    app.review("c1-new", "INT-2026-0107", "n1")
    app.review("c2-new", "INT-2026-0071", "n2")
    result = oracle.evaluate()
    assert result["success"] is True
    assert any("notification_rules_changed" in c for c in result["collateral"])


def test_missing_ticket_fails():
    app = ReviewApp()
    oracle = oracle_for(app)
    recreate(app)
    app.review("c1-new", "INT-2026-0107", "n1")
    app.review("c2-new", "INT-2026-0071", "n2")
    app.violations = [{"uuid": "v1"}, {"uuid": "v9"}]
    assert oracle.evaluate()["reason"] == "release_tickets_missing"
