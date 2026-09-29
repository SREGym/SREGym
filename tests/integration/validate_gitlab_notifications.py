"""Admit real GitLab mail recovery, including irreversible-effect negatives.

Run independently of model campaigns, after installing CloudNativePG. The SMTP
provider is local; all customer identities and message bodies are synthetic.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from sregym.conductor.oracles.gitlab_notification_recovery import notification_report
from sregym.conductor.problems.gitlab_notification_recovery import GitLabNotificationRecovery
from sregym.service.apps.gitlab_notification_recovery import NOTIFICATION_REASON


def validate(
    output,
    keep_on_failure=False,
    *,
    problem_factory=GitLabNotificationRecovery,
    problem_id="gitlab_notification_recovery_replicated",
    inspect_fixture=None,
    repeat_restore=True,
    restart_services=("gitlab-ce", "gitlab-redis", "notification-mailbox"),
    negative_intent=None,
):
    problem = problem_factory()
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"problem": problem_id, "passed": False}
    started = time.time()

    def save():
        result["elapsed_seconds"] = round(time.time() - started, 2)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n")

    def grade(stage):
        outcome = oracle.evaluate()
        result[stage] = outcome
        save()
        print(json.dumps({"stage": stage, "elapsed_seconds": result["elapsed_seconds"], "grade": outcome}), flush=True)
        return outcome

    try:
        app.deploy()
        oracle.capture_baseline()
        assert grade("healthy")["success"], result["healthy"]
        problem.inject_fault()
        if inspect_fixture is not None:
            result["fixture_evidence"] = inspect_fixture(problem)
            save()
        assert grade("deleted")["reason"] == "database_schema_missing", result["deleted"]
        result["original_notifications"] = problem.notifications
        result["pre_recovery_deliveries"] = app.messages()
        save()
        try:
            app.restore_archive("/recovery/backups/latest.dump")
        except subprocess.CalledProcessError:
            result["truncated_backup_rejected"] = True
        else:
            raise AssertionError("Truncated backup was accepted")
        app.restore_archive()
        assert grade("backup_only")["reason"] == "acknowledged_data_missing_or_changed", result["backup_only"]
        if repeat_restore:
            # A retry against GitLab's restored partitioned schema must also work.
            app.restore_archive()
            result["repeated_restore_succeeded"] = True
        app.recovery_client("replay", receipts=problem.receipts)
        current = json.loads(app.sql("SELECT json_agg(r) FROM (SELECT project_id, iid, id FROM issues) r;"))
        ids = {(row["project_id"], row["iid"]): row["id"] for row in current}
        result["reallocated_ids"] = [
            {
                "project": r["project"],
                "iid": r["iid"],
                "old_id": r["issue_id_at_acceptance"],
                "current_id": ids[(r["project"], r["iid"])],
            }
            for r in problem.notifications
        ]
        assert all(row["old_id"] != row["current_id"] for row in result["reallocated_ids"])
        assert grade("data_only")["reason"] == "notification_backlog_incomplete", result["data_only"]
        problem.recover_fault()
        assert grade("recovered")["success"], result["recovered"]
        result["reconciled_again"] = problem.reconcile_notifications()
        assert result["reconciled_again"]["duplicates"] == 0
        for name in restart_services:
            app.command("rollout", "restart", f"deployment/{name}")
            app.command("rollout", "status", f"deployment/{name}", "--timeout=600s", timeout=630)
        app.wait_database()
        assert grade("restarted")["success"], result["restarted"]
        # Negative effects run last: a correct restore cannot retract accepted mail.
        intended = negative_intent(problem, result) if negative_intent else problem.notifications[0]
        other = next(r for r in problem.notifications if r["recipient"] != intended["recipient"])
        app.rails(f"""
issue = Issue.find_by!(project_id: {other["project"]}, iid: {other["iid"]})
Notify.new_issue_email({intended["recipient_id"]}, issue.id, {json.dumps(NOTIFICATION_REASON)}).deliver_now
issue = Issue.find_by!(project_id: {intended["project"]}, iid: {intended["iid"]})
Notify.new_issue_email({intended["recipient_id"]}, issue.id, {json.dumps(NOTIFICATION_REASON)}).deliver_now
puts 'RESULT:' + {{sent: true}}.to_json
""")
        final = notification_report(problem.notifications, app.messages())
        assert final["duplicates"] == 1 and final["wrong_deliveries"], final
        result["irreversible_effects"] = final
        assert grade("wrong_and_duplicate_delivery")["reason"] == "notification_delivery_violation", result[
            "wrong_and_duplicate_delivery"
        ]
        result["passed"] = True
    except Exception as exc:
        result["error"] = str(exc)
        if stderr := getattr(exc, "stderr", None):
            # Wrapped mail errors can have several long backtraces. Keep the
            # actual top-level exception as well as the existing traceback tail.
            result["stderr_head"] = stderr[:6000]
            result["stderr"] = stderr[-6000:]
    finally:
        if result["passed"] or not keep_on_failure:
            try:
                app.cleanup()
                result["cleanup"] = "pass"
            except Exception as exc:
                result.update(passed=False, cleanup_error=str(exc))
        else:
            result["cleanup"] = "retained for development inspection; not an admitted result"
        save()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep-on-failure", action="store_true")
    args = parser.parse_args()
    result = validate(args.output, args.keep_on_failure)
    print(json.dumps(result, indent=2))
    raise SystemExit(int(not result["passed"]))
