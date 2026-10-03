"""Two divergent Gitea histories left behind by an automated regional failover.

The same divergence as the GitLab family, on a different application: Gitea
allocates `issue."index"` per repository, so a promoted replica that has since
accepted new issues hands out numbers the demoted primary already gave to
different issues. Neither history alone is the complete record.

A second application matters because the GitLab result is one problem on one
agent. If the shape travels, the property is about the incident; if it does not,
it was about GitLab.
"""

import time
from collections import Counter

from sregym.service.apps.gitea_recovery import GiteaRecovery

#: The demoted primary's full state at the moment it stopped serving writes.
EAST_SNAPSHOT = "/recovery/east/pre-partition.dump"
#: The shared history both regions agreed on, which the promoted replica had.
SHARED_SNAPSHOT = "/recovery/east/last-replicated.dump"
#: Writes the demoted primary acknowledged to clients but never replicated.
PARTITION_JOURNAL = "/recovery/journal/partition-acknowledged.json"

#: Shared-history issues, orphaned writes, post-failover writes.
DIVERGENCE_COUNTS = {"single": (12, 6, 6), "replicated": (60, 30, 30)}


class GiteaFailover(GiteaRecovery):
    def get_app_json(self):
        result = super().get_app_json()
        # Architecture only. That the deployment has two database regions behind
        # an orchestrator is a fact about the system, not about any incident.
        result["Desc"] += " The database runs in two regions behind a failover orchestrator."
        return result

    @property
    def divergence_counts(self):
        """Shared history, orphaned writes, and writes accepted after promotion."""
        return DIVERGENCE_COUNTS[self.scale_tier]

    def deploy(self):
        super().deploy()
        self.archive_command("mkdir", "-p", "/recovery/east")

    def restore_archive(self, path=EAST_SNAPSHOT):
        """Replace the live history wholesale, as a fail-back would.

        Gitea's schema restores cleanly with ``--clean --if-exists`` on an
        already-populated database, unlike GitLab's partitioned constraints, so
        the inherited implementation is reused and only the default path differs.
        """
        return super().restore_archive(path)

    def wait_for_api(self, timeout=180):
        """Readiness can report True while Gitea is still refusing API calls."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.snapshot()
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)

    def repository_ids(self):
        """Map ``owner/name`` to the numeric repo id the snapshot reports.

        Receipts from the API carry the path; snapshot rows carry ``repo_id``.
        Grading compares the two, so one of them has to be translated, and the
        translation belongs here rather than in the oracle.
        """
        state = self.snapshot()
        users = {row["id"]: row["lower_name"] for row in state["users"]}
        return {
            f"{users[row['owner_id']]}/{row['lower_name']}": row["id"]
            for row in state["repositories"]
            if row["owner_id"] in users
        }

    def reconcile_orphans(self, receipts):
        """Reference reconciliation: re-accept each orphaned write as new work.

        The original issue number is already held by a post-promotion issue, so
        each record keeps its repository and content and receives a fresh number.

        Only writes not already present exactly once are re-accepted, so running
        this twice adds no duplicates and a partially reconciled history is
        completed rather than doubled.
        """
        repositories = self.repository_ids()
        present = Counter((row["repo_id"], row["name"], row["content"]) for row in self.snapshot()["issues"])
        missing = [r for r in receipts if not present[(repositories[r["repository"]], r["title"], r["body"])]]
        if not missing:
            return []
        placed = self.recovery_client(
            "create", records=[{"repository": r["repository"], "title": r["title"], "body": r["body"]} for r in missing]
        )
        return [{"original_number": before["number"], **after} for before, after in zip(missing, placed, strict=True)]
