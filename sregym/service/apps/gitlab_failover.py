"""Two divergent GitLab histories left behind by an automated regional failover.

The live database is the promoted replica's history. The demoted primary's
history survives only as an independent snapshot plus an acknowledgement
journal, so neither side alone is the complete record.
"""

import time
from collections import Counter

from sregym.service.apps.gitlab_recovery import GitLabRecovery

#: The demoted primary's full state at the moment it stopped serving writes.
EAST_SNAPSHOT = "/recovery/east/pre-partition.dump"
#: The shared history both regions agreed on, which the promoted replica had.
SHARED_SNAPSHOT = "/recovery/east/last-replicated.dump"
#: Writes the demoted primary acknowledged to clients but never replicated.
PARTITION_JOURNAL = "/recovery/journal/partition-acknowledged.json"

#: Tenant projects, shared history issues, orphaned writes, post-failover writes.
DIVERGENCE_COUNTS = {"single": (3, 12, 6, 6), "replicated": (5, 60, 30, 30)}

FAILOVER_GUIDE = (
    """Regional failover divergence

A brief network partition between the two database regions triggered automated
failover. The replica in the surviving region was promoted and has been taking
writes ever since. The demoted primary had already acknowledged writes to clients
that never reached the promoted replica.

There are therefore two histories, and neither is complete:

- The live database holds the shared history plus every write accepted after
  promotion.
- The demoted primary's snapshot holds the shared history plus the writes it
  acknowledged during the partition. It does not contain anything accepted after
  promotion.

Both sets of writes were acknowledged to clients. Restoring the demoted
primary's snapshot over the live database would discard the post-promotion
writes; leaving it unused abandons the writes it acknowledged. Recover a single
history that retains both, without duplicating either.

Public issue identity is (project ID, issue IID). Because both regions kept
allocating IIDs after they diverged, the same IID was issued twice in the same
project for different issues.

Post-promotion issues keep the identity they already have; they are live and
already published, and reassigning them would break more clients than it fixes.
Each orphaned write therefore keeps its project and content and necessarily
receives a new IID. Recovery is judged on that outcome, not on how you reach it:
every orphaned write present exactly once, every post-promotion issue still at
its own IID with its own content, and the shared history untouched.

/recovery/east holds the demoted primary's snapshot and the last shared
snapshot, as read-only evidence. Do not modify them. The journal at
"""
    + PARTITION_JOURNAL
    + """
records the acknowledged-but-unreplicated writes in acceptance order.
/recovery/orchestrator.log records the failover decision.

The console includes PostgreSQL tools; its PG* environment targets this
application's writable service. Restore evidence into a scratch database if you
want to inspect it, never over the live one. Availability alone does not
complete this incident: a healthy endpoint can still be serving an incomplete
history. Fresh writes must work afterwards, and the configured PostgreSQL
membership and synchronous durability must be restored.
"""
)


class GitLabFailover(GitLabRecovery):
    @property
    def recovery_counts(self):
        projects, historical, orphaned, _ = DIVERGENCE_COUNTS[self.scale_tier]
        # The base class seeds tenants from [0] and its own tail from [2]; this
        # family drives both write phases itself.
        return projects, historical, orphaned

    @property
    def divergence_counts(self):
        """Orphaned writes on the demoted primary, and writes after promotion."""
        return DIVERGENCE_COUNTS[self.scale_tier][2:]

    def database_document(self):
        document = super().database_document()
        # Two full-schema restores in one attempt retain substantial catalog and
        # lock state; the inherited 1 GiB limit can OOM PostgreSQL mid-restore.
        document["spec"]["resources"]["requests"]["memory"] = "512Mi"
        document["spec"]["resources"]["limits"]["memory"] = "2Gi"
        return document

    def get_app_json(self):
        result = super().get_app_json()
        result["Desc"] += " Two histories diverged at an automated failover; see /recovery/failover.txt."
        return result

    def deploy(self):
        super().deploy()
        self.archive_command("mkdir", "-p", "/recovery/east")
        self.archive_write("/recovery/failover.txt", FAILOVER_GUIDE)

    def restore_archive(self, path=EAST_SNAPSHOT):
        """Replace the live history wholesale, as a fail-back would.

        Overridden because the inherited ``--clean --if-exists`` restore cannot
        individually drop GitLab's inherited and partitioned constraints on an
        already-populated database. Rebuilding the schemas with writers stopped
        reaches the same state the initial empty restore does.
        """
        self.archive_command("pg_restore", "--list", path)
        self.command("scale", "deployment/gitlab-ce", "--replicas=0")
        self.command("wait", "--for=delete", "pod", "-l", "app=gitlab-ce", "--timeout=180s", timeout=200)
        self.sql("""DO $$ DECLARE n text; BEGIN
          FOR n IN SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'
            AND nspname <> 'information_schema' LOOP
            EXECUTE format('DROP SCHEMA %I CASCADE', n);
          END LOOP;
          CREATE SCHEMA public AUTHORIZATION gitlab_ce;
        END $$;""")
        self.archive_command(
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--dbname=gitlab_ce",
            path,
            timeout=600,
        )
        self.wait_database()
        self.command("scale", "deployment/gitlab-ce", "--replicas=1")
        self.command(
            "rollout",
            "status",
            "deployment/gitlab-ce",
            f"--timeout={self.startup_timeout}s",
            timeout=self.startup_timeout + 30,
        )
        self.wait_for_api()

    def wait_for_api(self, timeout=180):
        """Readiness can report True while Puma is still refusing API calls."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.recovery_client("git")
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)

    def reconcile_orphans(self, receipts):
        """Reference reconciliation: re-accept each orphaned write as new work.

        The original IID is already held by a post-promotion issue, so each
        record keeps its project and content and receives a fresh IID. The
        returned mapping is what an operator would publish to affected clients.

        Only writes that are not already present exactly once are re-accepted,
        so running this twice adds no duplicates and a partially reconciled
        history is completed rather than doubled.
        """
        fields = ("project", "title", "description", "confidential")
        present = Counter(
            (row["project_id"], row["title"], row["description"], row["confidential"])
            for row in self.snapshot()["issues"]
        )
        missing = [r for r in receipts if not present[tuple(r[k] for k in fields)]]
        if not missing:
            return []
        placed = self.recovery_client("create", records=[{k: r[k] for k in fields} for r in missing])
        return [{"original_iid": before["iid"], **after} for before, after in zip(missing, placed, strict=True)]
