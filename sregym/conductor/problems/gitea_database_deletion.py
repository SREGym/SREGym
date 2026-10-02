"""An accidental primary schema deletion, with backup selection and a recovery tail."""

import json
import secrets
import time

from sregym.conductor.oracles.gitea_database_recovery import GiteaDatabaseRecoveryOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.gitea import FIXTURES
from sregym.service.apps.gitea_recovery import ARCHIVE, JOURNAL, RECOVERY_COUNTS, GiteaRecovery
from sregym.utils.decorators import mark_fault_injected


class GiteaDatabaseDeletion(Problem):
    def __init__(self, scale_tier="replicated"):
        super().__init__(GiteaRecovery(tier=scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service = "gitea"
        self.expected_service_port = 3000
        self.expected = None
        self.expected_git = None
        self.archive_sha256 = None
        self.receipts = []
        self.deletion_attempted = False
        self.root_cause = self.build_structured_root_cause(
            component="gitea-db",
            namespace=self.namespace,
            description=(
                "A maintenance reset intended for a disposable database ran against Gitea's writable primary "
                "and deleted the public schema. Physical replicas replayed the deletion. The most recent "
                "logical backup is truncated; the older complete archive predates acknowledged issue creations. "
                "Recovery requires restoring retained state and reconciling the independent issue journal."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = GiteaDatabaseRecoveryOracle(problem=self)
        self.app.create_workload()

    def prepare_recovery_material(self):
        app = self.app
        if self.expected is not None:
            raise RuntimeError("Refusing to replace an existing incident's recovery material")
        historical, recent = RECOVERY_COUNTS[app.scale_tier]
        fixture = json.loads((FIXTURES / "import-data.json").read_text())
        repositories = [r["owner"] + "/" + r["name"] for r in fixture["repositories"]]
        nonce = secrets.token_hex(8)

        def records(count, phase):
            return [
                {
                    "repository": repositories[index % len(repositories)],
                    "title": f"{phase} request {index + 1} ({nonce})",
                    "body": f"Accepted change request {nonce}/{phase}/{index + 1}. Preserve this issue's URL and content.",
                }
                for index in range(count)
            ]

        app.recovery_client("create", records=records(historical, "Historical"))
        app.archive_command("pg_dump", "--format=custom", "--file=" + ARCHIVE)
        app.archive_command("pg_restore", "--list", ARCHIVE)
        self.archive_sha256 = app.archive_command("sha256sum", ARCHIVE).split()[0]
        daily_time = time.time()
        self.receipts = app.recovery_client("create", records=records(recent, "Recent"))
        app.archive_write(JOURNAL, json.dumps(self.receipts, indent=2) + "\n")
        latest = "/recovery/backups/latest.dump"
        app.archive_command("pg_dump", "--format=custom", "--file=" + latest)
        latest_sha = app.archive_command("sha256sum", latest).split()[0]
        # Model an upload reporting completion while persisting only the header.
        # No complete recent archive is left anywhere in the environment.
        app.archive_command("truncate", "-s", "32", latest)
        app.archive_write(
            "/recovery/backups/catalog.json",
            json.dumps(
                [
                    {
                        "file": "daily.dump",
                        "completed_at": daily_time,
                        "status": "completed",
                        "sha256": self.archive_sha256,
                    },
                    {"file": "latest.dump", "completed_at": time.time(), "status": "completed", "sha256": latest_sha},
                ],
                indent=2,
            )
            + "\n",
        )
        self.expected = app.snapshot()
        self.expected_git = app.git_inventory()
        # Ensure that even the asynchronous third member has received the tail
        # before deleting state. This is a setup gate, not a grader grace period.
        deadline = time.monotonic() + 60
        while any(app.snapshot(pod=p["metadata"]["name"]) != self.expected for p in app.database_pods()):
            if time.monotonic() >= deadline:
                raise RuntimeError("Pre-incident acknowledged records did not reach every PostgreSQL member")
            time.sleep(1)

    @mark_fault_injected
    def inject_fault(self):
        self.prepare_recovery_material()
        primary = self.app.cluster()["status"]["currentPrimary"]
        self.app.archive_write(
            "/recovery/operations.log",
            "maintenance: requested disposable database reset\n"
            f"session: namespace={self.namespace} host=gitea-db-rw database=gitea server={primary}\n"
            "statement: DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION gitea;\n",
        )
        self.deletion_attempted = True
        self.app.sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION gitea;")
        deadline = time.monotonic() + 60
        while any(
            self.app.sql("SELECT to_regclass('public.issue') IS NULL;", pod=p["metadata"]["name"]) != "t"
            for p in self.app.database_pods()
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError("The deletion did not propagate to all configured database members")
            time.sleep(1)

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        # Safe to call after a partial injection and during final cleanup. Do not
        # replace a state that an agent has already recovered successfully.
        if not self.deletion_attempted:
            return
        if not self.mitigation_oracle.evaluate().get("success"):
            self.app.restore_archive()
            self.app.recovery_client("replay", receipts=self.receipts)
        self.deletion_attempted = False
