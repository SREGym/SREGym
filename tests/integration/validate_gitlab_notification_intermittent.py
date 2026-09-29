"""Admit safe real GitLab recovery through an ongoing SMTP impairment."""

import argparse
import json
from pathlib import Path

from sregym.conductor.problems.gitlab_notification_intermittent import GitLabNotificationIntermittent
from tests.integration.validate_gitlab_notification_ambiguity import inspect_fixture as inspect_ambiguity
from tests.integration.validate_gitlab_notifications import validate


def inspect_fixture(problem):
    evidence = inspect_ambiguity(problem)
    evidence["ongoing_fault"] = problem.app.ongoing_report()
    assert evidence["ongoing_fault"] == {"active": True, "attempts": 0}
    return evidence


def select_negative_intent(problem, evidence):
    # Called after successful recovery, a second reconciliation, provider
    # restart and fresh-work grading. The transport fault must still be active.
    evidence["ongoing_fault_after_restart"] = problem.app.ongoing_report()
    evidence["transport_after_recovery"] = problem.app.transport_report()
    evidence["reference_delivery_history"] = problem.reference_delivery_history
    assert evidence["ongoing_fault_after_restart"]["active"]
    assert evidence["ongoing_fault_after_restart"]["attempts"] >= 21
    assert evidence["reference_delivery_history"][0]["delivery_errors"]
    assert evidence["reference_delivery_history"][1]["sends"] == 0
    for key in ("accepted_without_ack", "failed_without_acceptance"):
        assert evidence["transport_after_recovery"][key] > evidence["fixture_evidence"]["transport"][key]
    # The existing final negative probes deliberately send a duplicate and a
    # wrong-recipient message. Make those two sends deterministic; all recovery
    # and restart checks above ran with the ongoing fault still enabled.
    problem.app.ongoing_fault(False)
    evidence["transport_disabled_only_for_final_negative_probes"] = True
    return evidence["fixture_evidence"]["accepted_but_queued"][0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep-on-failure", action="store_true")
    args = parser.parse_args()
    result = validate(
        args.output,
        args.keep_on_failure,
        problem_factory=GitLabNotificationIntermittent,
        problem_id="gitlab_notification_intermittent_replicated",
        inspect_fixture=inspect_fixture,
        repeat_restore=False,
        restart_services=("notification-mailbox",),
        negative_intent=select_negative_intent,
    )
    print(json.dumps(result, indent=2))
    raise SystemExit(int(not result["passed"]))
