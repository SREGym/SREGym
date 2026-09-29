"""Grade recovered business state, not a prescribed restore command sequence."""

import time
from collections import Counter

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.gitea import GiteaOracle
from sregym.service.apps.gitea_recovery import ARCHIVE


def compare_records(expected, actual):
    """Allow fresh work while requiring every original identity and value."""
    result = {}
    for table, keys in {
        "users": ("id",),
        "repositories": ("id",),
        "issues": ("repo_id", "index"),
    }.items():
        original = {tuple(row[k] for k in keys): row for row in expected[table]}
        current = {tuple(row[k] for k in keys): row for row in actual[table]}
        missing = sum(key not in current for key in original)
        changed = sum(key in current and current[key] != row for key, row in original.items())
        result[table] = {"expected": len(original), "missing": missing, "changed": changed}
    # An accidental replay into new issue numbers must not turn into duplicates.
    original_counts = Counter((r["repo_id"], r["name"], r["content"]) for r in expected["issues"])
    current_counts = Counter((r["repo_id"], r["name"], r["content"]) for r in actual["issues"])
    result["duplicate_issues"] = sum(max(0, current_counts[key] - count) for key, count in original_counts.items())
    return result


def records_intact(report):
    return not report["duplicate_issues"] and all(
        not report[name][key] for name in ("users", "repositories", "issues") for key in ("missing", "changed")
    )


class GiteaDatabaseRecoveryOracle(GiteaOracle):
    replica_convergence_timeout_seconds = 30
    FAILURE_CLASSES = {
        "database_schema_missing": FailureClass.AGENT_ERROR,
        "acknowledged_data_missing_or_changed": FailureClass.AGENT_ERROR,
        "acknowledged_git_data_changed": FailureClass.AGENT_ERROR,
        "recovery_archive_changed": FailureClass.AGENT_ERROR,
    }

    def evaluate(self):
        problem = self.problem
        if not self.baseline or problem.expected is None:
            return super().evaluate()
        accounts = {}
        try:
            app = problem.app
            primary = app.cluster()["status"]["currentPrimary"]
            for pod in app.database_pods():
                name = pod["metadata"]["name"]
                if app.sql("SELECT to_regclass('public.issue') IS NULL;", pod=name) == "t":
                    return self.fail("database_schema_missing", member=name)
                accounts[name] = compare_records(problem.expected, app.snapshot(pod=name))
                deadline = time.monotonic() + self.replica_convergence_timeout_seconds
                while name != primary and not records_intact(accounts[name]) and time.monotonic() < deadline:
                    time.sleep(1)
                    accounts[name] = compare_records(problem.expected, app.snapshot(pod=name))
                if not records_intact(accounts[name]):
                    return self.fail("acknowledged_data_missing_or_changed", members=accounts)
            if app.archive_command("sha256sum", ARCHIVE).split()[0] != problem.archive_sha256:
                return self.fail("recovery_archive_changed")
            result = super().evaluate()
            if not result.get("success"):
                return result
            actual_git = app.git_inventory()
            for repository, files in problem.expected_git.items():
                if any(actual_git.get(repository, {}).get(path) != digest for path, digest in files.items()):
                    return self.fail("acknowledged_git_data_changed", repository=repository)
            app.recovery_client("verify", receipts=problem.receipts)
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            **result,
            "recovered_records": accounts,
            "journal_entries_verified": len(problem.receipts),
            "acknowledged_data_loss": 0,
        }
