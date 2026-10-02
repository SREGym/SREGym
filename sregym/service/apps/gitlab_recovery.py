"""Real GitLab database recovery, independent archives and issue reconciliation."""

import json
from pathlib import Path

from sregym.service.apps.gitea import POSTGRES_IMAGE
from sregym.service.apps.gitlab_ce import GitLabCE

ARCHIVE = "/recovery/backups/staging.dump"
JOURNAL = "/recovery/journal/acknowledged-issues.json"
COUNTS = {"single": (3, 12, 6), "replicated": (5, 60, 30)}
SNAPSHOT_SQL = """
SELECT json_build_object(
 'users', (SELECT coalesce(json_agg(r), '[]') FROM
   (SELECT id, username, email, state, admin FROM users ORDER BY id) r),
 'projects', (SELECT coalesce(json_agg(r), '[]') FROM
   (SELECT id, namespace_id, name, path, visibility_level FROM projects ORDER BY id) r),
 'members', (SELECT coalesce(json_agg(r), '[]') FROM
   (SELECT id, source_id, source_type, user_id, access_level FROM members ORDER BY id) r),
 'issues', (SELECT coalesce(json_agg(r), '[]') FROM
   (SELECT project_id, iid, title, description, author_id, confidential, state_id
      FROM issues ORDER BY project_id, iid) r)
);
"""


class GitLabRecovery(GitLabCE):
    data_volumes = (*GitLabCE.data_volumes, "gitlab-recovery-archives")

    @property
    def recovery_counts(self):
        """Tenant projects, historical issues and acknowledged recovery-tail issues."""
        return COUNTS[self.scale_tier]

    def database_document(self):
        document = super().database_document()
        # GitLab's many partitions/indexes need a larger lock table for an
        # atomic schema reset and pg_restore --single-transaction.
        document["spec"]["postgresql"]["parameters"]["max_locks_per_transaction"] = "1024"
        return document

    def get_app_json(self):
        result = super().get_app_json()
        result["Desc"] += " The recovery-console pod mounts the database archive volume at /recovery."
        return result

    def render(self):
        documents = super().render()
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "recovery-console"},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {"fsGroup": 26},
                    "volumes": [
                        {"name": "archives", "persistentVolumeClaim": {"claimName": "gitlab-recovery-archives"}}
                    ],
                    "containers": [
                        {
                            "name": "tools",
                            "image": POSTGRES_IMAGE,
                            "command": ["sleep", "infinity"],
                            "env": [
                                {"name": k, "value": v}
                                for k, v in {
                                    "PGHOST": "gitlab-ce-db-rw",
                                    "PGUSER": "gitlab_ce",
                                    "PGDATABASE": "gitlab_ce",
                                    "PGSSLMODE": "require",
                                    "PGCONNECT_TIMEOUT": "5",
                                }.items()
                            ]
                            + [self.secret_env("PGPASSWORD", "password", "application-database")],
                            "volumeMounts": [{"name": "archives", "mountPath": "/recovery"}],
                            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "512Mi"}},
                        }
                    ],
                },
            }
        )
        return documents

    def deploy(self):
        super().deploy()
        self.command("wait", "--for=condition=Ready", "pod/recovery-console", "--timeout=300s", timeout=320)
        self.archive_command("mkdir", "-p", "/recovery/backups", "/recovery/journal")
        marker = self.command("get", "configmap", "recovery-fixtures", "--ignore-not-found", "-o", "json")
        if not marker.strip():
            self.projects = self.recovery_client("seed", count=self.recovery_counts[0])
            self.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "recovery-fixtures"},
                        "data": {"projects": json.dumps(self.projects)},
                    }
                ]
            )
        else:
            self.projects = json.loads(json.loads(marker)["data"]["projects"])

    def archive_command(self, *args, input_text=None, timeout=300):
        return self.command("exec", "-i", "recovery-console", "--", *args, input_text=input_text, timeout=timeout)

    def archive_write(self, path, content):
        self.archive_command("sh", "-c", 'cat > "$1"', "write-evidence", path, input_text=content)

    def recovery_client(self, mode, **arguments):
        directory = Path(__file__).parent
        source = (directory / "saas_workflows.py").read_text() + "\n"
        source += (directory / "gitlab_recovery_workflow.py").read_text()
        source += f"\nrecovery_workflow(Client('gitlab-ce'), {mode!r}, **json.loads({json.dumps(arguments)!r}))\n"
        return json.loads(
            self.command("exec", "-i", "application-client", "--", "python", "-", input_text=source, timeout=600)
        )

    def snapshot(self, pod=None):
        return json.loads(self.sql(SNAPSHOT_SQL, pod=pod))

    def git_inventory(self):
        return self.recovery_client("git")

    def restore_archive(self, path=ARCHIVE):
        self.archive_command("pg_restore", "--list", path)
        self.command("scale", "deployment/gitlab-ce", "--replicas=0")
        self.command("wait", "--for=delete", "pod", "-l", "app=gitlab-ce", "--timeout=180s", timeout=200)
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
