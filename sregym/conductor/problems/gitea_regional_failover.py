"""The GitHub-2018 divergence on Gitea, to test whether the shape travels.

`gitlab_regional_failover` is the sharpest problem in the suite at 1 of 3, and it
is one problem on one agent. This is the same incident on a different
application: if a frontier agent fails here too, the difficulty is a property of
the divergence; if it solves this, the GitLab result was about GitLab.

Gitea allocates `issue."index"` per repository, so the promoted replica issues
numbers the demoted primary already gave to different issues -- the same
collision GitLab's IIDs produce, reached by a different route.
"""

import json
import secrets
import time

from sregym.conductor.oracles.gitea_divergent_recovery import GiteaDivergentRecoveryOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.gitea import FIXTURES
from sregym.service.apps.gitea_failover import (
    EAST_SNAPSHOT,
    PARTITION_JOURNAL,
    SHARED_SNAPSHOT,
    GiteaFailover,
)
from sregym.utils.decorators import mark_fault_injected


class GiteaRegionalFailover(Problem):
    application_class = GiteaFailover
    #: Reference reconciliation runs after grading and re-accepts a whole tail.
    cleanup_timeout_seconds = 600

    def __init__(self, scale_tier="replicated"):
        super().__init__(self.application_class(tier=scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service, self.expected_service_port = "gitea", 3000
        self.shared = None
        self.orphaned = []
        self.accepted_after_promotion = []
        self.expected_git = None
        self.repository_ids = {}
        self.snapshot_sha256 = {}
        self.failover_performed = False
        self.root_cause = self.build_structured_root_cause(
            component="gitea-db",
            namespace=self.namespace,
            description="A brief inter-region partition made the orchestrator promote a lagging replica. "
            "Issues the demoted primary had already acknowledged were never replicated and are absent from the "
            "promoted history, which has since accepted new issues under the same per-repository numbers. "
            "Retain both acknowledged sets in one history instead of failing back or abandoning the orphans.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = GiteaDivergentRecoveryOracle(problem=self)
        self.app.create_workload()

    def records(self, count, phase, nonce):
        fixture = json.loads((FIXTURES / "import-data.json").read_text())
        repositories = [r["owner"] + "/" + r["name"] for r in fixture["repositories"]]
        return [
            {
                "repository": repositories[index % len(repositories)],
                "title": f"{phase} {nonce}/{index}",
                "body": f"Acknowledged customer change {nonce}/{phase}/{index}",
            }
            for index in range(count)
        ]

    def prepare_divergence_material(self):
        """Build the shared history, then the two acknowledged tails.

        Order matters: the shared snapshot is taken before the orphaned writes so
        restoring it reproduces exactly what the promoted replica had.
        """
        if self.shared is not None:
            raise RuntimeError("Incident divergence material already exists")
        app = self.app
        historical, orphan_count, _ = app.divergence_counts
        nonce = secrets.token_hex(8)

        app.recovery_client("create", records=self.records(historical, "shared", nonce))
        app.archive_command("pg_dump", "--format=custom", "--file=" + SHARED_SNAPSHOT)
        app.archive_command("pg_restore", "--list", SHARED_SNAPSHOT)
        self.shared = app.snapshot()
        self.repository_ids = app.repository_ids()

        # Issues the demoted primary acknowledged during the partition. They are
        # in the live database now and vanish when the replica is promoted.
        self.orphaned = app.recovery_client("create", records=self.records(orphan_count, "partition", nonce))
        app.archive_command("pg_dump", "--format=custom", "--file=" + EAST_SNAPSHOT)
        app.archive_command("pg_restore", "--list", EAST_SNAPSHOT)
        app.archive_write(PARTITION_JOURNAL, json.dumps(self.orphaned, indent=2) + "\n")
        for label, path in (("shared", SHARED_SNAPSHOT), ("demoted_primary", EAST_SNAPSHOT)):
            self.snapshot_sha256[label] = app.archive_command("sha256sum", path).split()[0]

    def write_evidence(self, promoted):
        # The orchestrator's own log, and nothing authored at the responder. A
        # failover tool really does record what it did; a colleague writing down
        # the diagnosis is a briefing by another name.
        self.app.archive_write(
            "/recovery/orchestrator.log",
            "region-east primary unreachable for 43s; health checks failed in 3 consecutive probes\n"
            "failover policy: promote most advanced reachable replica in region-west\n"
            f"promoted {promoted}; region-east demoted, writes refused\n"
            "replication lag at promotion: unknown\n",
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
        app.wait_for_api()
        if app.snapshot() != self.shared:
            raise RuntimeError("Promotion did not reproduce the last replicated history")

        # New work accepted after promotion. These reuse the issue numbers the
        # demoted primary had already issued, so the two histories collide.
        _, _, post_count = app.divergence_counts
        nonce = secrets.token_hex(8)
        self.accepted_after_promotion = app.recovery_client(
            "create", records=self.records(post_count, "post-failover", nonce)
        )
        collisions = {(r["repository"], r["number"]) for r in self.orphaned} & {
            (r["repository"], r["number"]) for r in self.accepted_after_promotion
        }
        if not collisions:
            raise RuntimeError("The two histories did not reuse any public issue number")
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
