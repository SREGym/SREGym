"""Require one Gitea history that retains both regions' acknowledged writes.

The accounting is the GitLab family's, reused with Gitea's column names: the
divergence is a property of the incident, not of the application, so grading it
twice from scratch would only create two things to keep in step.

The base class is `GiteaOracle`, not `SaaSOracle`. `SaaSOracle.capture_baseline`
calls `app.record_query(token)`, which GitLab, Mattermost and Stripe define and
Gitea does not -- inheriting it fails at the *deploy* stage with a bare
`AttributeError`, which reads as a broken application rather than a wrong base
class. `GiteaOracle` supplies the Gitea equivalents (`check_workflow`,
`volume_ids`, `verify_replication`), which is why the deletion family uses it.
"""

import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.gitea import GiteaOracle
from sregym.conductor.oracles.gitlab_divergent_recovery import divergence_report
from sregym.service.apps.gitea_failover import EAST_SNAPSHOT, SHARED_SNAPSHOT


def gitea_keys(repositories):
    """Content, identity and label accessors for Gitea issues.

    ``repositories`` maps ``owner/name`` to the numeric repo id, because API
    receipts carry the path and snapshot rows carry the id.
    """

    def repo(record):
        if "repo_id" in record:
            return record["repo_id"]
        return repositories[record["repository"]]

    def content(record):
        if "repo_id" in record:
            return (repo(record), record["name"], record["content"])
        return (repo(record), record["title"], record["body"])

    def identity(record):
        number = record["index"] if "index" in record else record["number"]
        return (repo(record), number)

    def label(record):
        return record["title"] if "title" in record else record["name"]

    return content, identity, label


class GiteaDivergentRecoveryOracle(GiteaOracle):
    FAILURE_CLASSES = {
        "database_schema_missing": FailureClass.AGENT_ERROR,
        "post_promotion_writes_lost": FailureClass.AGENT_ERROR,
        "orphaned_writes_abandoned": FailureClass.AGENT_ERROR,
        "acknowledged_writes_duplicated": FailureClass.AGENT_ERROR,
        "shared_history_changed": FailureClass.AGENT_ERROR,
        "recovery_evidence_changed": FailureClass.AGENT_ERROR,
        "acknowledged_git_data_changed": FailureClass.AGENT_ERROR,
    }
    replica_convergence_timeout_seconds = 30

    @classmethod
    def verdict(cls, report):
        """Name the specific way a recovery fell short, most decisive first."""
        if report["shared_history_displaced"]:
            return cls.fail("shared_history_changed", divergence=report)
        if report["post_promotion_displaced"]:
            # The signature of a fail-back: the promoted history's own writes are
            # gone, or their numbers now carry the demoted primary's content.
            return cls.fail("post_promotion_writes_lost", divergence=report)
        if report["orphaned_duplicated"] or report["post_promotion_duplicated"]:
            return cls.fail("acknowledged_writes_duplicated", divergence=report)
        if report["orphaned_missing"]:
            return cls.fail("orphaned_writes_abandoned", divergence=report)
        return None

    def report_for(self, pod=None):
        problem, app = self.problem, self.problem.app
        keys = gitea_keys(problem.repository_ids)
        return divergence_report(
            problem.shared["issues"],
            problem.orphaned,
            problem.accepted_after_promotion,
            app.snapshot(pod=pod)["issues"],
            keys=keys,
        )

    def evaluate(self):
        problem, app = self.problem, self.problem.app
        if not self.baseline or problem.shared is None:
            return super().evaluate()
        members = {}
        try:
            primary = app.cluster()["status"]["currentPrimary"]
            for pod in app.database_pods():
                name = pod["metadata"]["name"]
                if app.sql("SELECT to_regclass('public.issue') IS NULL;", pod=name) == "t":
                    return self.fail("database_schema_missing", member=name)
                report = self.report_for(pod=name)
                # A replica may still be replaying a reconciliation the primary
                # has already committed; only the primary is graded immediately.
                deadline = time.monotonic() + self.replica_convergence_timeout_seconds
                while name != primary and self.verdict(report) and time.monotonic() < deadline:
                    time.sleep(1)
                    report = self.report_for(pod=name)
                members[name] = report
                if failed := self.verdict(report):
                    return {**failed, "member": name}

            for label, path in (("shared", SHARED_SNAPSHOT), ("demoted_primary", EAST_SNAPSHOT)):
                if app.archive_command("sha256sum", path).split()[0] != problem.snapshot_sha256[label]:
                    return self.fail("recovery_evidence_changed", snapshot=label)
            git = app.git_inventory()
            for repository, files in problem.expected_git.items():
                if any(git.get(repository, {}).get(path) != digest for path, digest in files.items()):
                    return self.fail("acknowledged_git_data_changed", repository=repository)
            result = super().evaluate()
            if not result.get("success"):
                return result
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            **result,
            "divergence": members,
            "orphaned_writes_recovered": len(problem.orphaned),
            "post_promotion_writes_retained": len(problem.accepted_after_promotion),
            "acknowledged_data_loss": 0,
        }
