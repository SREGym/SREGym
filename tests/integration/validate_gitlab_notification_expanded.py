"""Admit the expanded GitLab workload before attempting model calibration."""

import argparse
import json
from pathlib import Path

from sregym.conductor.problems.gitlab_notification_expanded import GitLabNotificationExpanded
from tests.integration.validate_gitlab_notification_delayed_audit import inspect_fixture as inspect_delayed
from tests.integration.validate_gitlab_notification_intermittent import select_negative_intent as select_intermittent
from tests.integration.validate_gitlab_notifications import validate


def inspect_fixture(problem):
    evidence = inspect_delayed(problem)
    assert len(problem.app.projects) == 20
    assert len(problem.notifications) == len(problem.receipts) == 120
    assert evidence["notifications"]["missing"] == 111
    assert evidence["retained_incident_jobs"] == 114
    assert evidence["durable_audit_records"] >= 1609
    evidence["workload_counts"] = problem.app.recovery_counts
    return evidence


def select_negative_intent(problem, evidence):
    publication = evidence["publication_after_restart"] = problem.app.publication_report()
    assert publication["delayed_deliveries"] == 111, publication
    assert publication["minimum_delay"] == publication["maximum_delay"] == publication["policy_delay"] == 30.0
    first, repeated = problem.reference_delivery_history
    # A long batch may publish some early acceptances before it ends. Require
    # evidence of the delayed tail, without imposing a particular batch speed.
    assert first["delayed_acceptances"] > 0, first
    assert first["batches"] == [111, 28, 7, 2], first
    assert repeated["sends"] == 0 and repeated["delayed_acceptances"] == 0
    for run in (first, repeated):
        assert all(c["complete_through"] >= c["barrier"] for c in run["watermark_checks"])
    return select_intermittent(problem, evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep-on-failure", action="store_true")
    args = parser.parse_args()
    result = validate(
        args.output,
        args.keep_on_failure,
        problem_factory=GitLabNotificationExpanded,
        problem_id="gitlab_notification_delayed_audit_expanded",
        inspect_fixture=inspect_fixture,
        repeat_restore=False,
        restart_services=("notification-mailbox",),
        negative_intent=select_negative_intent,
    )
    print(json.dumps(result, indent=2))
    raise SystemExit(int(not result["passed"]))
