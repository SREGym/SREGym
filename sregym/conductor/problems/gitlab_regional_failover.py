"""GitHub-2018-inspired regional failover leaving two acknowledged histories.

A brief partition triggers automated promotion of a lagging replica. The demoted
primary had already acknowledged writes that never replicated, and the promoted
replica keeps accepting new writes afterwards. Both sets were acknowledged to
clients, and the two histories reused the same public issue identities, so no
single restore recovers the incident.
"""

import json
import secrets
import time

from sregym.conductor.oracles.gitlab_divergent_recovery import GitLabDivergentRecoveryOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.gitlab_failover import (
    EAST_SNAPSHOT,
    PARTITION_JOURNAL,
    SHARED_SNAPSHOT,
    GitLabFailover,
)
from sregym.utils.decorators import mark_fault_injected


class GitLabRegionalFailover(Problem):
    application_class = GitLabFailover
    #: Reference reconciliation runs after grading and re-accepts a whole tail.
    cleanup_timeout_seconds = 900

    def __init__(self, scale_tier="replicated"):
        super().__init__(self.application_class(scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service, self.expected_service_port = "gitlab-ce", 80
        self.shared = None
        self.orphaned = []
        self.accepted_after_promotion = []
        self.expected_git = None
        self.snapshot_sha256 = {}
        self.failover_performed = False
        self.root_cause = self.build_structured_root_cause(
            component="gitlab-ce-db",
            namespace=self.namespace,
            description="A brief inter-region partition made the orchestrator promote a lagging replica. "
            "Writes the demoted primary had already acknowledged were never replicated and are absent from the "
            "promoted history, which has since accepted new writes under the same public issue identities. "
            "Retain both acknowledged sets in one history instead of failing back or abandoning the orphans.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = GitLabDivergentRecoveryOracle(self)
        self.app.create_workload()

    def records(self, count, phase, nonce):
        app = self.app
        return [
            {
                "project": app.projects[i % len(app.projects)],
                "title": f"{phase} {nonce}/{i}",
                "description": f"Acknowledged customer change {nonce}/{phase}/{i}",
                "confidential": bool(i % 2),
            }
            for i in range(count)
        ]

    def prepare_divergence_material(self):
        """Build the shared history, then the two acknowledged tails.

        Order matters. The shared snapshot is taken before the orphaned writes so
        that restoring it reproduces exactly what the promoted replica had.
        """
        if self.shared is not None:
            raise RuntimeError("Incident divergence material already exists")
        app = self.app
        _, historical, orphan_count = app.recovery_counts
        nonce = secrets.token_hex(8)

        app.recovery_client("create", records=self.records(historical, "shared", nonce))
        app.archive_command("pg_dump", "--format=custom", "--file=" + SHARED_SNAPSHOT)
        app.archive_command("pg_restore", "--list", SHARED_SNAPSHOT)
        self.shared = app.snapshot()

        # Writes the demoted primary acknowledged during the partition. They are
        # in the live database now and vanish when the replica is promoted.
        self.orphaned = app.recovery_client("create", records=self.records(orphan_count, "partition", nonce))
        app.archive_command("pg_dump", "--format=custom", "--file=" + EAST_SNAPSHOT)
        app.archive_command("pg_restore", "--list", EAST_SNAPSHOT)
        app.archive_write(PARTITION_JOURNAL, json.dumps(self.orphaned, indent=2) + "\n")
        for label, path in (("shared", SHARED_SNAPSHOT), ("demoted_primary", EAST_SNAPSHOT)):
            self.snapshot_sha256[label] = app.archive_command("sha256sum", path).split()[0]

    def write_evidence(self, promoted):
        app = self.app
        app.archive_write(
            "/recovery/orchestrator.log",
            "region-east primary unreachable for 43s; health checks failed in 3 consecutive probes\n"
            "failover policy: promote most advanced reachable replica in region-west\n"
            f"promoted {promoted}; region-east demoted, writes refused\n"
            "replication lag at promotion: unknown; replica was behind the primary\n"
            "NOTE: the demoted primary's unreplicated writes were not discarded by this tool.\n",
        )
        app.archive_write(
            "/recovery/chat.txt",
            "on-call: some issues customers opened during the blip are gone from the UI\n"
            "on-call: but issues opened since the failover are fine, and new ones work\n"
            "dba: do NOT restore the east snapshot over the live database, we have taken writes since\n"
            "support: two customers say their issue URL now shows somebody else's issue\n"
            "storage: repository files were never affected\n",
        )
        app.archive_write(
            "/recovery/ticket.txt",
            "Account for every write both regions acknowledged. Issues opened during the partition are "
            "missing, and some of their URLs now resolve to different issues opened after the failover. "
            "Neither set may be lost or duplicated. A healthy /-/health is not recovery.\n",
        )

    @mark_fault_injected
    def inject_fault(self):
        self.prepare_divergence_material()
        app = self.app
        self.write_evidence(app.cluster()["status"]["currentPrimary"])

        # Promotion of the lagging replica: the live history reverts to the last
        # replicated state, so the acknowledged partition writes disappear.
        self.failover_performed = True
        app.restore_archive(SHARED_SNAPSHOT)
        if app.snapshot() != self.shared:
            raise RuntimeError("Promotion did not reproduce the last replicated history")

        # New work accepted after promotion. These reuse the IIDs the demoted
        # primary had already issued, so the two histories collide.
        _, post_count = app.divergence_counts
        nonce = secrets.token_hex(8)
        self.accepted_after_promotion = app.recovery_client(
            "create", records=self.records(post_count, "post-failover", nonce)
        )
        collisions = {(r["project"], r["iid"]) for r in self.orphaned} & {
            (r["project"], r["iid"]) for r in self.accepted_after_promotion
        }
        if not collisions:
            raise RuntimeError("The two histories did not reuse any public issue identity")
        self.expected_git = app.git_inventory()
        promoted_history = app.snapshot()
        deadline = time.monotonic() + 60
        while any(app.snapshot(pod=p["metadata"]["name"]) != promoted_history for p in app.database_pods()):
            if time.monotonic() >= deadline:
                raise RuntimeError("Post-promotion state did not reach every database member")
            time.sleep(1)

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        if self.failover_performed and not self.mitigation_oracle.evaluate().get("success"):
            self.app.wait_for_api()
            self.app.reconcile_orphans(self.orphaned)
        self.failover_performed = False
