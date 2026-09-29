"""Admit ambiguous real SMTP acceptance and recovery through a paginated audit."""

import argparse
import json
from pathlib import Path

from sregym.conductor.oracles.gitlab_notification_recovery import notification_report
from sregym.conductor.problems.gitlab_notification_ambiguity import GitLabNotificationAmbiguity
from tests.integration.validate_gitlab_notifications import validate


def inspect_fixture(problem):
    app = problem.app
    public, private = app.public_messages(), app.messages()
    assert public == private, "Pagination omitted durable evidence"
    report = notification_report(problem.notifications, public)
    assert report["delivered"] == 9 and report["missing"] == len(problem.notifications) - 9, report
    assert report["duplicates"] == 0 and not report["wrong_deliveries"], report
    transport = app.transport_report()
    assert transport["accepted_without_ack"] == 3, transport
    # The accepted-but-unacknowledged messages came from jobs that still exist
    # in the real mailers queue/retry sets. Public journal IDs identify them.
    jobs = problem.retained_incident_jobs
    assert len(jobs) == len(problem.notifications) - 6, len(jobs)
    accepted_queued = [
        record
        for record in problem.notifications
        if record["job_id"] and notification_report([record], public)["delivered"]
    ]
    assert len(accepted_queued) == 3
    serialized = json.dumps(jobs)
    assert all(record["job_id"] in serialized for record in accepted_queued)
    return {
        "notifications": report,
        "transport": transport,
        "durable_audit_records": len(public),
        "retained_incident_jobs": len(jobs),
        "accepted_but_queued": accepted_queued,
        "public_audit_matches_protected_ledger": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep-on-failure", action="store_true")
    args = parser.parse_args()
    result = validate(
        args.output,
        args.keep_on_failure,
        problem_factory=GitLabNotificationAmbiguity,
        problem_id="gitlab_notification_ambiguity_replicated",
        inspect_fixture=inspect_fixture,
        # The unchanged v3 restore/restart path already passed full admission.
        # Recheck the new persistent provider, including its ambiguous delivery.
        repeat_restore=False,
        restart_services=("notification-mailbox",),
        negative_intent=lambda problem, evidence: evidence["fixture_evidence"]["accepted_but_queued"][0],
    )
    print(json.dumps(result, indent=2))
    raise SystemExit(int(not result["passed"]))
