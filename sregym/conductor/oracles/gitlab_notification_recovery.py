"""Grade recipient-visible effects as well as the restored GitLab database."""

import secrets
import time
from collections import Counter

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.gitlab_database_recovery import GitLabDatabaseRecoveryOracle
from sregym.service.apps.gitlab_notification_recovery import NOTIFICATION_REASON


def notification_report(expected, messages, reason=NOTIFICATION_REASON):
    """Public identity and intended content matter; SQL row IDs may change."""
    by_identity = {(str(r["project"]), str(r["iid"])): r for r in expected}
    counts, wrong = Counter(), []
    for message in messages:
        headers = message["headers"]
        if headers.get("x-gitlab-notificationreason") != reason:
            continue
        identity = (headers.get("x-gitlab-project-id"), headers.get("x-gitlab-issue-iid"))
        receipt = by_identity.get(identity)
        if (
            receipt is None
            or message["recipients"] != [receipt["recipient"]]
            or receipt["title"] not in message["subject"]
            or receipt["description"] not in message["body"]
        ):
            wrong.append(message["id"])
        else:
            counts[identity] += 1
    return {
        "expected": len(expected),
        "delivered": sum(bool(counts[key]) for key in by_identity),
        "missing": sum(not counts[key] for key in by_identity),
        "duplicates": sum(max(0, count - 1) for count in counts.values()),
        "wrong_deliveries": wrong,
    }


def complete(report):
    return not (report["missing"] or report["duplicates"] or report["wrong_deliveries"])


class GitLabNotificationRecoveryOracle(GitLabDatabaseRecoveryOracle):
    FAILURE_CLASSES = {
        "notification_delivery_violation": FailureClass.AGENT_ERROR,
        "notification_backlog_incomplete": FailureClass.AGENT_ERROR,
        "notification_jobs_unreconciled": FailureClass.AGENT_ERROR,
        "fresh_notification_failed": FailureClass.AGENT_ERROR,
    }

    def evaluate(self):
        result = super().evaluate()
        if not result.get("success"):
            return result
        app, problem = self.problem.app, self.problem
        try:
            report = notification_report(problem.notifications, app.messages())
            if report["duplicates"] or report["wrong_deliveries"]:
                return self.fail("notification_delivery_violation", notifications=report)
            if report["missing"]:
                return self.fail("notification_backlog_incomplete", notifications=report)
            probe_reason = "sregym-probe-" + secrets.token_hex(8)
            probe = app.recovery_client(
                "create",
                records=[
                    {
                        "project": app.projects[0],
                        "title": probe_reason,
                        "description": "Fresh notification " + probe_reason,
                        "confidential": True,
                    }
                ],
            )[0]
            probe["recipient"] = "incident-user-1@sregym.local"
            jobs = app.enqueue_notification_probe(probe, probe_reason)
            if jobs["pending_incident_jobs"]:
                return self.fail("notification_jobs_unreconciled", pending=jobs["pending_incident_jobs"])
            deadline = time.monotonic() + 90
            while True:
                fresh = notification_report([probe], app.messages(), reason=probe_reason)
                if complete(fresh):
                    break
                if time.monotonic() >= deadline:
                    return self.fail("fresh_notification_failed", notifications=fresh)
                time.sleep(2)
            # The fresh job also gives already-running work time to finish.
            report = notification_report(problem.notifications, app.messages())
            if not complete(report):
                return self.fail("notification_delivery_violation", notifications=report)
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {**result, "notifications": report, "fresh_notification_verified": True}
