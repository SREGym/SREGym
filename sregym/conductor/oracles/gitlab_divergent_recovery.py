"""Require one history that retains both regions' acknowledged writes.

Two obvious actions each fail here, for their own recorded reason: restoring the
demoted primary's snapshot discards everything accepted after promotion, and
leaving it unused abandons the writes it acknowledged.
"""

import time
from collections import Counter

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.saas import SaaSOracle
from sregym.service.apps.gitlab_failover import EAST_SNAPSHOT, SHARED_SNAPSHOT


def content_key(record):
    """Content key from either a snapshot row or an acknowledgement receipt."""
    project = record["project_id"] if "project_id" in record else record["project"]
    return (project, record["title"], record["description"], record["confidential"])


def identity_key(record):
    project = record["project_id"] if "project_id" in record else record["project"]
    return (project, record["iid"])


def divergence_report(shared, orphaned, post_promotion, issues):
    """Account for every acknowledged write in the recovered history.

    ``orphaned`` writes are graded on content, not identity: their original IIDs
    now belong to post-promotion issues, so a correct recovery necessarily gives
    them new ones. ``post_promotion`` and ``shared`` writes are graded on both,
    because nothing legitimately moves them.
    """
    counts = Counter(content_key(row) for row in issues)
    identities = {identity_key(row): row for row in issues}

    def displaced(receipts):
        out = []
        for receipt in receipts:
            row = identities.get(identity_key(receipt))
            if row is None or content_key(row) != content_key(receipt):
                out.append({"project": identity_key(receipt)[0], "iid": receipt["iid"]})
        return out

    return {
        "shared_history_displaced": displaced(shared),
        "post_promotion_displaced": displaced(post_promotion),
        "post_promotion_duplicated": sum(max(0, counts[content_key(r)] - 1) for r in post_promotion),
        "orphaned_missing": [r["title"] for r in orphaned if not counts[content_key(r)]],
        "orphaned_duplicated": [r["title"] for r in orphaned if counts[content_key(r)] > 1],
        "orphaned_recovered": sum(counts[content_key(r)] == 1 for r in orphaned),
    }


def both_histories_retained(report):
    return not any(
        report[key]
        for key in (
            "shared_history_displaced",
            "post_promotion_displaced",
            "post_promotion_duplicated",
            "orphaned_missing",
            "orphaned_duplicated",
        )
    )


class GitLabDivergentRecoveryOracle(SaaSOracle):
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
        """Name the specific way a recovery fell short, most decisive first.

        A classmethod because ``fail`` is one: the mapping from a report to a
        reason code is worth testing without a cluster.
        """
        if report["shared_history_displaced"]:
            return cls.fail("shared_history_changed", divergence=report)
        if report["post_promotion_displaced"]:
            # The signature of a fail-back: the promoted history's own writes are
            # gone or their identities now carry the demoted primary's content.
            return cls.fail("post_promotion_writes_lost", divergence=report)
        if report["orphaned_duplicated"] or report["post_promotion_duplicated"]:
            return cls.fail("acknowledged_writes_duplicated", divergence=report)
        if report["orphaned_missing"]:
            return cls.fail("orphaned_writes_abandoned", divergence=report)
        return None

    def evaluate(self):
        problem, app = self.problem, self.problem.app
        if not self.baseline or problem.shared is None:
            return super().evaluate()
        members = {}
        try:
            primary = app.cluster()["status"]["currentPrimary"]
            for pod in app.database_pods():
                name = pod["metadata"]["name"]
                if app.sql("SELECT to_regclass('public.issues') IS NULL;", pod=name) == "t":
                    return self.fail("database_schema_missing", member=name)
                report = divergence_report(
                    problem.shared["issues"],
                    problem.orphaned,
                    problem.accepted_after_promotion,
                    app.snapshot(pod=name)["issues"],
                )
                # A replica may still be replaying a reconciliation the primary
                # has already committed; only the primary is graded immediately.
                deadline = time.monotonic() + self.replica_convergence_timeout_seconds
                while name != primary and not both_histories_retained(report) and time.monotonic() < deadline:
                    time.sleep(1)
                    report = divergence_report(
                        problem.shared["issues"],
                        problem.orphaned,
                        problem.accepted_after_promotion,
                        app.snapshot(pod=name)["issues"],
                    )
                members[name] = report
                if failed := self.verdict(report):
                    return {**failed, "member": name}

            for label, path in (("shared", SHARED_SNAPSHOT), ("demoted_primary", EAST_SNAPSHOT)):
                if app.archive_command("sha256sum", path).split()[0] != problem.snapshot_sha256[label]:
                    return self.fail("recovery_evidence_changed", snapshot=label)
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
            "divergence": members,
            "orphaned_writes_recovered": len(problem.orphaned),
            "post_promotion_writes_retained": len(problem.accepted_after_promotion),
            "acknowledged_data_loss": 0,
        }
