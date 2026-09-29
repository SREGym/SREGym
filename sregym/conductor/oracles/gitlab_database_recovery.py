"""Require recovered identities, access settings, issue history and Git content."""

import time
from collections import Counter

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.saas import SaaSOracle
from sregym.service.apps.gitlab_recovery import ARCHIVE

KEYS = {"users": ("id",), "projects": ("id",), "members": ("id",), "issues": ("project_id", "iid")}


def compare_records(expected, actual):
    report = {}
    for table, keys in KEYS.items():
        original = {tuple(row[k] for k in keys): row for row in expected[table]}
        current = {tuple(row[k] for k in keys): row for row in actual[table]}
        report[table] = {
            "expected": len(original),
            "missing": sum(key not in current for key in original),
            "changed": sum(key in current and current[key] != row for key, row in original.items()),
        }

    def counts(rows):
        return Counter((r["project_id"], r["title"], r["description"], r["confidential"]) for r in rows)

    before, after = counts(expected["issues"]), counts(actual["issues"])
    report["duplicate_issues"] = sum(max(0, after[key] - count) for key, count in before.items())
    return report


def records_intact(report):
    return not report["duplicate_issues"] and all(
        not report[table][key] for table in KEYS for key in ("missing", "changed")
    )


class GitLabDatabaseRecoveryOracle(SaaSOracle):
    FAILURE_CLASSES = {
        "database_schema_missing": FailureClass.AGENT_ERROR,
        "acknowledged_data_missing_or_changed": FailureClass.AGENT_ERROR,
        "acknowledged_git_data_changed": FailureClass.AGENT_ERROR,
        "recovery_archive_changed": FailureClass.AGENT_ERROR,
    }
    replica_convergence_timeout_seconds = 30

    def evaluate(self):
        problem, app = self.problem, self.problem.app
        if not self.baseline or problem.expected is None:
            return super().evaluate()
        report = {}
        try:
            primary = app.cluster()["status"]["currentPrimary"]
            for pod in app.database_pods():
                name = pod["metadata"]["name"]
                if app.sql("SELECT to_regclass('public.issues') IS NULL;", pod=name) == "t":
                    return self.fail("database_schema_missing", member=name)
                report[name] = compare_records(problem.expected, app.snapshot(pod=name))
                deadline = time.monotonic() + self.replica_convergence_timeout_seconds
                while name != primary and not records_intact(report[name]) and time.monotonic() < deadline:
                    time.sleep(1)
                    report[name] = compare_records(problem.expected, app.snapshot(pod=name))
                if not records_intact(report[name]):
                    return self.fail("acknowledged_data_missing_or_changed", members=report)
            if app.archive_command("sha256sum", ARCHIVE).split()[0] != problem.archive_sha256:
                return self.fail("recovery_archive_changed")
            app.recovery_client("verify", receipts=problem.receipts)
            git = app.git_inventory()
            for project, files in problem.expected_git.items():
                if any(git.get(project, {}).get(path) != digest for path, digest in files.items()):
                    return self.fail("acknowledged_git_data_changed", project=project)
            result = super().evaluate()
            if not result.get("success"):
                return result
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            **result,
            "recovered_records": report,
            "journal_entries_verified": len(problem.receipts),
            "acknowledged_data_loss": 0,
        }
