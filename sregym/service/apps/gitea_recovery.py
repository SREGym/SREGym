"""Gitea's database-loss environment with durable, independently stored archives."""

import json
import subprocess
from pathlib import Path

from sregym.service.apps.gitea import FIXTURES, POSTGRES_IMAGE, Gitea

RECOVERY_COUNTS = {"single": (12, 6), "replicated": (60, 30)}
ARCHIVE = "/recovery/backups/daily.dump"
JOURNAL = "/recovery/journal/acknowledged-issues.json"

# Stable business identities and content; intentionally excludes volatile timestamps
# and internal issue IDs, which may change during logical API replay.
SNAPSHOT_SQL = """
SELECT json_build_object(
 'users', (SELECT coalesce(json_agg(r), '[]') FROM
   (SELECT id, lower_name, email, is_active, is_admin FROM "user" ORDER BY id) r),
 'repositories', (SELECT coalesce(json_agg(r), '[]') FROM
   (SELECT id, owner_id, lower_name, is_private FROM repository ORDER BY id) r),
 'issues', (SELECT coalesce(json_agg(r), '[]') FROM
   (SELECT repo_id, "index", name, content, poster_id, is_closed, is_pull
      FROM issue ORDER BY repo_id, "index") r)
);
"""


class GiteaRecovery(Gitea):
    @property
    def expected_volume_count(self):
        return super().expected_volume_count + 1

    def get_app_json(self):
        metadata = super().get_app_json()
        metadata["Desc"] += " The recovery-console pod mounts the database archive volume at /recovery."
        return metadata

    def render(self):
        documents = super().render()
        documents += [
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "gitea-recovery-archives"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": self.storage_class,
                    "resources": {"requests": {"storage": "1Gi"}},
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "recovery-console"},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {"fsGroup": 26},
                    "volumes": [
                        {"name": "archives", "persistentVolumeClaim": {"claimName": "gitea-recovery-archives"}}
                    ],
                    "containers": [
                        {
                            "name": "tools",
                            "image": POSTGRES_IMAGE,
                            "command": ["sleep", "infinity"],
                            "env": [
                                {"name": key, "value": value}
                                for key, value in {
                                    "PGHOST": "gitea-db-rw",
                                    "PGUSER": "gitea",
                                    "PGDATABASE": "gitea",
                                    "PGSSLMODE": "require",
                                }.items()
                            ]
                            + [
                                {
                                    "name": "PGPASSWORD",
                                    "valueFrom": {"secretKeyRef": {"name": "gitea-database", "key": "password"}},
                                }
                            ],
                            "volumeMounts": [{"name": "archives", "mountPath": "/recovery"}],
                            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "256Mi"}},
                        }
                    ],
                },
            },
        ]
        return documents

    def deploy(self):
        super().deploy()
        self.command("wait", "--for=condition=Ready", "pod/recovery-console", "--timeout=300s", timeout=320)
        self.archive_command("mkdir", "-p", "/recovery/backups", "/recovery/journal")

    def archive_command(self, *args, input_text=None, timeout=180):
        return self.command(
            "exec",
            "-i",
            "recovery-console",
            "-c",
            "tools",
            "--",
            *args,
            input_text=input_text,
            timeout=timeout,
        )

    def archive_write(self, path, content):
        # The path is an argv argument; shell interpolation never sees file content.
        self.archive_command("sh", "-c", 'cat > "$1"', "write-archive", path, input_text=content)

    def recovery_client(self, mode, **arguments):
        directory = Path(__file__).parent
        source = (directory / "gitea_workflow.py").read_text()
        source += "\n" + (directory / "gitea_recovery_workflow.py").read_text()
        source += "\nrun_recovery(api, " + repr(mode) + ", **json.loads(" + repr(json.dumps(arguments)) + "))\n"
        output = self.command("exec", "-i", "application-client", "--", "python", "-", input_text=source, timeout=300)
        return json.loads(output)

    def snapshot(self, pod=None):
        return json.loads(self.sql(SNAPSHOT_SQL, pod=pod))

    def git_inventory(self):
        result = {}
        for repository in json.loads((FIXTURES / "import-data.json").read_text())["repositories"]:
            name = repository["owner"] + "/" + repository["name"]
            try:
                listing = self.command(
                    "exec",
                    "deployment/gitea",
                    "--",
                    "su-exec",
                    "git",
                    "git",
                    "--git-dir",
                    f"/data/git/repositories/{name}.git",
                    "ls-tree",
                    "-r",
                    "HEAD",
                )
            except subprocess.CalledProcessError:
                # A repository whose storage is gone is missing git data, which is
                # a graded outcome. Raising here instead would surface a real
                # recovery failure as an environment error and void the attempt.
                result[name] = {}
                continue
            result[name] = {line.split("\t", 1)[1]: line.split("\t", 1)[0] for line in listing.splitlines()}
        return result

    #: A local snapshot of the repository storage, on the same volume it protects.
    GIT_BACKUP = "/data/backups/repositories.tar.gz"

    def backup_git_repositories(self):
        self.command(
            "exec",
            "deployment/gitea",
            "--",
            "sh",
            "-c",
            f"mkdir -p $(dirname {self.GIT_BACKUP}) && tar czf {self.GIT_BACKUP} -C /data/git repositories",
            timeout=300,
        )

    def restore_git_repositories(self):
        self.command(
            "exec",
            "deployment/gitea",
            "--",
            "sh",
            "-c",
            f"tar xzf {self.GIT_BACKUP} -C /data/git && chown -R git:git /data/git/repositories",
            timeout=300,
        )

    def discard_repository_storage(self, repository):
        """Remove one repository's git storage, leaving the database intact."""
        self.command(
            "exec",
            "deployment/gitea",
            "--",
            "sh",
            "-c",
            f"rm -rf /data/git/repositories/{repository}.git",
            timeout=120,
        )

    def pause_application(self):
        self.command("scale", "deployment/gitea", "--replicas=0")
        self.command("wait", "--for=delete", "pod", "-l", "app=gitea", "--timeout=180s", timeout=200)

    def resume_application(self):
        self.command("scale", "deployment/gitea", "--replicas=1")
        self.command("rollout", "status", "deployment/gitea", "--timeout=300s", timeout=320)

    def restore_archive(self, path=ARCHIVE):
        # Validate before quiescing writers or issuing destructive restore options.
        self.archive_command("pg_restore", "--list", path)
        self.pause_application()
        self.archive_command(
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--dbname=gitea",
            path,
        )
        self.wait_database()
        self.resume_application()
