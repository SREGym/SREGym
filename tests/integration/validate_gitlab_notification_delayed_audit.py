"""Admit GitLab recovery with ambiguous SMTP and delayed public receipts."""

import argparse
import json
from pathlib import Path

from sregym.conductor.problems.gitlab_notification_delayed_audit import GitLabNotificationDelayedAudit
from tests.integration.validate_gitlab_notification_intermittent import inspect_fixture as inspect_intermittent
from tests.integration.validate_gitlab_notification_intermittent import select_negative_intent as select_intermittent
from tests.integration.validate_gitlab_notifications import validate


def inspect_fixture(problem):
    evidence = inspect_intermittent(problem)
    evidence["publication"] = problem.app.publication_report()
    assert evidence["publication"] == {
        "delayed_deliveries": 0,
        "minimum_delay": None,
        "maximum_delay": None,
        "policy_delay": 30.0,
    }
    return evidence


def select_negative_intent(problem, evidence):
    evidence["publication_after_restart"] = problem.app.publication_report()
    publication = evidence["publication_after_restart"]
    assert publication["delayed_deliveries"] == 21, publication
    assert publication["minimum_delay"] == publication["maximum_delay"] == publication["policy_delay"] == 30.0
    first, repeated = problem.reference_delivery_history
    assert first["delayed_acceptances"] == 21, first
    assert first["audit_wait_seconds"] >= 30.0 * 3
    assert first["batches"] == [21, 5, 2], first
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
        problem_factory=GitLabNotificationDelayedAudit,
        problem_id="gitlab_notification_delayed_audit_replicated",
        inspect_fixture=inspect_fixture,
        repeat_restore=False,
        restart_services=("notification-mailbox",),
        negative_intent=select_negative_intent,
    )
    print(json.dumps(result, indent=2))
    raise SystemExit(int(not result["passed"]))
