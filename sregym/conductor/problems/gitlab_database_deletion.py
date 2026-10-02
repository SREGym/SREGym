"""GitLab-2017-inspired wrong-primary deletion with broken backup and recovery tail."""

import json
import secrets
import time

from sregym.conductor.oracles.gitlab_database_recovery import GitLabDatabaseRecoveryOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.gitlab_recovery import ARCHIVE, JOURNAL, GitLabRecovery
from sregym.utils.decorators import mark_fault_injected


class GitLabDatabaseDeletion(Problem):
    application_class = GitLabRecovery

    def __init__(self, scale_tier="replicated"):
        super().__init__(self.application_class(scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service, self.expected_service_port = "gitlab-ce", 80
        self.expected = self.expected_git = self.archive_sha256 = None
        self.receipts, self.deletion_attempted = [], False
        self.root_cause = self.build_structured_root_cause(
            component="gitlab-ce-db",
            namespace=self.namespace,
            description="Replica maintenance used the writable-primary endpoint and erased application schemas. "
            "Physical standbys replayed the deletion. The advertised latest backup is truncated; a valid staging "
            "snapshot predates accepted issue creations. Recover identities, permissions, Git data and the acknowledged tail.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = GitLabDatabaseRecoveryOracle(self)
        self.app.create_workload()

    def prepare_recovery_material(self):
        if self.expected is not None:
            raise RuntimeError("Incident recovery material already exists")
        app = self.app
        _, historical, recent = app.recovery_counts
        nonce = secrets.token_hex(8)

        def records(count, phase):
            return [
                {
                    "project": app.projects[i % len(app.projects)],
                    "title": f"{phase} {nonce}/{i}",
                    "description": f"Acknowledged customer change {nonce}/{phase}/{i}",
                    "confidential": bool(i % 2),
                }
                for i in range(count)
            ]

        app.recovery_client("create", records=records(historical, "historical"))
        app.archive_command("pg_dump", "--format=custom", "--file=" + ARCHIVE)
        app.archive_command("pg_restore", "--list", ARCHIVE)
        self.archive_sha256 = app.archive_command("sha256sum", ARCHIVE).split()[0]
        self.before_acknowledged_writes()
        self.receipts = app.recovery_client("create", records=records(recent, "recent"))
        app.archive_write(JOURNAL, json.dumps(self.receipts, indent=2) + "\n")
        latest = "/recovery/backups/latest.dump"
        app.archive_command("pg_dump", "--format=custom", "--file=" + latest)
        checksum = app.archive_command("sha256sum", latest).split()[0]
        app.archive_command("truncate", "-s", "32", latest)
        app.archive_write(
            "/recovery/backups/catalog.json",
            json.dumps(
                [
                    {"file": "nightly.dump", "status": "scheduled"},
                    {"file": "staging.dump", "status": "completed", "sha256": self.archive_sha256},
                    {"file": "latest.dump", "status": "completed", "sha256": checksum},
                ],
                indent=2,
            ),
        )
        self.expected, self.expected_git = app.snapshot(), app.git_inventory()
        deadline = time.monotonic() + 60
        while any(app.snapshot(pod=p["metadata"]["name"]) != self.expected for p in app.database_pods()):
            if time.monotonic() >= deadline:
                raise RuntimeError("Acknowledged state did not reach every database member before injection")
            time.sleep(1)

    def before_acknowledged_writes(self):
        """Optional independent-system changes after the database snapshot."""

    @mark_fault_injected
    def inject_fault(self):
        self.prepare_recovery_material()
        app = self.app
        self.deletion_attempted = True
        app.sql("""DO $$ DECLARE n text; BEGIN
          FOR n IN SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'
            AND nspname <> 'information_schema' LOOP
            EXECUTE format('DROP SCHEMA %I CASCADE', n);
          END LOOP;
          CREATE SCHEMA public AUTHORIZATION gitlab_ce;
        END $$;""")
        deadline = time.monotonic() + 60
        while any(
            app.sql("SELECT to_regclass('public.issues') IS NULL;", pod=p["metadata"]["name"]) != "t"
            for p in app.database_pods()
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError("Database deletion failed to reach every member")
            time.sleep(1)

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        if self.deletion_attempted and not self.mitigation_oracle.evaluate().get("success"):
            self.app.restore_archive()
            self.app.recovery_client("replay", receipts=self.receipts)
        self.deletion_attempted = False
