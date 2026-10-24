"""Gitea on two regional MySQL servers, with a webhook consumer in the second region."""

import base64
import json
import secrets
import subprocess
import time
from collections import Counter
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.mysql import REGIONS, MySQLRegions, other
from sregym.service.apps.gitea.application import FIXTURES, GiteaApplication
from sregym.service.apps.gitea.failover import GiteaFailoverApplication
from sregym.service.apps.gitea.recovery import GiteaRecoveryApplication

#: Shared-history issues, orphaned writes, post-failover writes.
DIVERGENCE_COUNTS = {"small": (30, 12, 12), "large": (200, 80, 80)}
RECEIVER = "gitea-webhook-receiver"
RECEIVER_IMAGE = "python:3.12.13-alpine3.23"
DELIVER_TIMEOUT = 5
LIVE_TRAFFIC = "gitea-users"
LIVE_TRAFFIC_REPOSITORY = "zoo-labs/zoo-utilities"
LIVE_TRAFFIC_INTERVAL = 2

# Rows are sorted in Python: JSON_ARRAYAGG does not guarantee order.
SNAPSHOT_SQL = """
SELECT JSON_OBJECT(
 'users', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('id', id, 'lower_name', lower_name)), JSON_ARRAY())
             FROM `user`),
 'repositories', (SELECT COALESCE(JSON_ARRAYAGG(
                    JSON_OBJECT('id', id, 'owner_id', owner_id, 'lower_name', lower_name)), JSON_ARRAY())
                    FROM repository),
 'issues', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT(
              'id', id, 'repo_id', repo_id, 'index', `index`, 'name', name, 'content', content,
              'is_pull', is_pull, 'is_closed', is_closed)), JSON_ARRAY())
              FROM issue),
 'comments', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('id', id, 'issue_id', issue_id, 'content', content)),
                JSON_ARRAY())
                FROM comment WHERE type = 0)
);
"""


class GiteaMySQLApplication(GiteaApplication):
    metadata_file = Path(__file__).with_name("metadata-mysql.json")
    recovery_client = GiteaRecoveryApplication.recovery_client
    git_inventory = GiteaRecoveryApplication.git_inventory
    repository_ids = GiteaFailoverApplication.repository_ids

    def __init__(self, tier="small", storage_class="openebs-hostpath"):
        if tier not in DIVERGENCE_COUNTS:
            raise ValueError(f"Unknown tier {tier!r}; choose from {tuple(DIVERGENCE_COUNTS)}")
        super().__init__("single", storage_class)
        self.scale_tier = tier
        self.divergence_counts = DIVERGENCE_COUNTS[tier]
        self.description = self.get_app_json()["Desc"]

    def get_app_json(self):
        return Application.get_app_json(self)

    @property
    def mysql(self):
        return MySQLRegions(self.command, "gitea-db", "gitea", self.storage_class, "gitea-mysql")

    @property
    def expected_volume_count(self):
        return 1 + len(REGIONS)

    def render(self):
        documents = [d for d in super().render() if d["kind"] != "Cluster"]
        for document in documents:
            if document["kind"] == "Deployment":
                metadata = document["spec"]["template"]["metadata"]
            elif document["kind"] == "Pod":
                metadata = document["metadata"]
            else:
                continue
            metadata["labels"] = {**metadata.get("labels", {}), "region": "east"}
        container = next(d for d in documents if d["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e for e in container["env"] if e["name"] != "GITEA__database__SSL_MODE"}
        for name, value in {
            "GITEA__database__DB_TYPE": "mysql",
            "GITEA__database__HOST": f"{self.mysql.write_service}:3306",
            "GITEA__webhook__ALLOWED_HOST_LIST": RECEIVER,
            "GITEA__webhook__DELIVER_TIMEOUT": str(DELIVER_TIMEOUT),
        }.items():
            env[name] = {"name": name, "value": value}
        container["env"] = list(env.values())
        labels = {"app": RECEIVER, "region": "west"}
        receiver = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": RECEIVER},
                "data": {"receiver.py": Path(__file__).with_name("webhook_receiver.py").read_text()},
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": RECEIVER},
                "spec": {"selector": labels, "ports": [{"port": 80, "targetPort": 8080}]},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": RECEIVER},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "volumes": [{"name": "receiver", "configMap": {"name": RECEIVER}}],
                            "containers": [
                                {
                                    "name": "receiver",
                                    "image": RECEIVER_IMAGE,
                                    "command": ["python", "-u", "/receiver/receiver.py"],
                                    "env": [
                                        {
                                            "name": "WEBHOOK_SECRET",
                                            "valueFrom": {"secretKeyRef": {"name": "gitea-webhook", "key": "secret"}},
                                        }
                                    ],
                                    "ports": [{"containerPort": 8080}],
                                    "volumeMounts": [{"name": "receiver", "mountPath": "/receiver"}],
                                    "readinessProbe": {"tcpSocket": {"port": 8080}, "periodSeconds": 5},
                                    "resources": {
                                        "requests": {"cpu": "10m", "memory": "32Mi"},
                                        "limits": {"memory": "128Mi"},
                                    },
                                }
                            ],
                        },
                    },
                },
            },
        ]
        return [*self.mysql.render("gitea-database"), *documents, *receiver]

    def ensure_secret(self, name, data):
        if not self.command("get", "secret", name, "--ignore-not-found", "-o", "name").strip():
            self.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": name},
                        "type": "Opaque",
                        "stringData": data,
                    }
                ]
            )

    def secret_value(self, name, key):
        secret = json.loads(self.command("get", "secret", name, "-o", "json"))
        value = base64.b64decode(secret["data"][key]).decode()
        # Interpolated into SQL and passed to the client; generated as hex below.
        if not value or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"Secret {name}/{key} is not a generated hex value")
        return value

    def deploy_database(self, documents):
        self.ensure_secret(
            # MySQL limits replication passwords to 32 characters.
            "gitea-mysql",
            {"root-password": secrets.token_hex(24), "replication-password": secrets.token_hex(16)},
        )
        self.ensure_secret("gitea-webhook", {"secret": secrets.token_hex(24)})
        database = [
            d
            for d in documents
            if d["kind"] in ("Service", "StatefulSet") and d["metadata"].get("labels", {}).get("app") == "gitea-db"
        ]
        self.apply(database)
        self.mysql.wait_ready()
        self.mysql.initialize(self.secret_value("gitea-mysql", "replication-password"))
        return database

    def deploy(self):
        super().deploy()
        self.command("rollout", "status", f"deployment/{RECEIVER}", "--timeout=300s", timeout=320)
        repositories = [
            r["owner"] + "/" + r["name"]
            for r in json.loads((FIXTURES / "import-data.json").read_text())["repositories"]
        ]
        self.recovery_client(
            "webhooks",
            repositories=repositories,
            url=f"http://{RECEIVER}/",
            secret=self.secret_value("gitea-webhook", "secret"),
        )

    def live_traffic_documents(self):
        labels = {"app": LIVE_TRAFFIC, "region": "east"}
        return [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": LIVE_TRAFFIC},
                "data": {"users.py": Path(__file__).with_name("live_traffic.py").read_text()},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": LIVE_TRAFFIC},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "volumes": [
                                {"name": "users", "configMap": {"name": LIVE_TRAFFIC}},
                                {"name": "credentials", "secret": {"secretName": "gitea-admin"}},
                            ],
                            "containers": [
                                {
                                    "name": "users",
                                    "image": RECEIVER_IMAGE,
                                    "command": ["python", "-u", "/users/users.py"],
                                    "env": [
                                        {"name": "REPOSITORY", "value": LIVE_TRAFFIC_REPOSITORY},
                                        {"name": "INTERVAL", "value": str(LIVE_TRAFFIC_INTERVAL)},
                                    ],
                                    "volumeMounts": [
                                        {"name": "users", "mountPath": "/users"},
                                        {"name": "credentials", "mountPath": "/credentials", "readOnly": True},
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "10m", "memory": "32Mi"},
                                        "limits": {"memory": "128Mi"},
                                    },
                                }
                            ],
                        },
                    },
                },
            },
        ]

    def start_live_traffic(self):
        self.apply(self.live_traffic_documents())
        self.command("rollout", "status", f"deployment/{LIVE_TRAFFIC}", "--timeout=300s", timeout=320)

    def pod_identities(self, selector):
        pods = json.loads(self.command("get", "pods", "-l", selector, "-o", "json"))["items"]
        return [(p["metadata"]["uid"], sum(c["restartCount"] for c in p["status"]["containerStatuses"])) for p in pods]

    def database_identity(self, region):
        """The server's pod, its restarts and its data volume; any change means the server was taken down."""
        pod = json.loads(self.command("get", "pod", self.mysql.pod(region), "-o", "json"))
        claims = [
            v["persistentVolumeClaim"]["claimName"] for v in pod["spec"]["volumes"] if "persistentVolumeClaim" in v
        ]
        volumes = [json.loads(self.command("get", "pvc", c, "-o", "json"))["metadata"]["uid"] for c in claims]
        restarts = sum(c["restartCount"] for c in pod["status"]["containerStatuses"])
        return [pod["metadata"]["uid"], restarts, volumes]

    def live_traffic(self, action=None):
        request = (
            f"urllib.request.Request('http://127.0.0.1:8080/{action}', method='POST')"
            if action
            else ("'http://127.0.0.1:8080/'")
        )
        output = self.command(
            "exec",
            f"deployment/{LIVE_TRAFFIC}",
            "--",
            "python",
            "-c",
            f"import urllib.request; print(urllib.request.urlopen({request}).read().decode())",
        )
        return None if action else json.loads(output)

    def snapshot(self, region=None):
        state = json.loads(self.mysql.sql(region or self.mysql.write_region(), SNAPSHOT_SQL))
        state["users"].sort(key=lambda r: r["id"])
        state["repositories"].sort(key=lambda r: r["id"])
        state["issues"] = sorted(
            (r for r in state["issues"] if not r["is_pull"]), key=lambda r: (r["repo_id"], r["index"])
        )
        state["comments"].sort(key=lambda r: r["id"])
        return state

    def client_fetch(self, url):
        """Fetch a URL from the region-east client pod, as Gitea would."""
        script = "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3).read()"
        try:
            self.command("exec", "application-client", "--", "python", "-c", script, url, timeout=30)
        except subprocess.CalledProcessError:
            return False
        return True

    def receiver_reachable(self):
        return self.client_fetch(f"http://{RECEIVER}/")

    def wait_for_api(self, timeout=180):
        deadline = time.monotonic() + timeout
        while not self.client_fetch("http://gitea:3000/api/v1/version"):
            if time.monotonic() >= deadline:
                raise RuntimeError("The Gitea API did not become available")
            time.sleep(3)

    def restart_gitea(self):
        self.command("rollout", "restart", "deployment/gitea")
        self.command("rollout", "status", "deployment/gitea", "--timeout=600s", timeout=620)
        self.wait_for_api()

    def undelivered_webhooks(self, region):
        return int(self.mysql.sql(region, "SELECT COUNT(*) FROM hook_task WHERE is_delivered = 0;"))

    def wait_for_webhook_attempts(self, region):
        # Partitioned deliveries each wait out DELIVER_TIMEOUT before failing.
        deadline = time.monotonic() + max(300, (DELIVER_TIMEOUT + 1) * self.undelivered_webhooks(region) + 120)
        while self.undelivered_webhooks(region):
            if time.monotonic() >= deadline:
                raise RuntimeError("Gitea did not attempt every queued webhook delivery")
            time.sleep(3)
        # Gitea marks a task delivered before sending it; let the last requests finish or time out.
        time.sleep(2 * DELIVER_TIMEOUT + 5)

    def issue_counters_behind(self, region):
        """Repositories whose next issue number would collide with an existing one."""
        return int(
            self.mysql.sql(
                region,
                "SELECT COUNT(*) FROM (SELECT repo_id, MAX(`index`) AS top FROM issue GROUP BY repo_id) i "
                "LEFT JOIN issue_index x ON x.group_id = i.repo_id WHERE x.max_index IS NULL OR x.max_index < i.top;",
            )
        )

    def issue_statistics_stale(self, region):
        """Issues and repositories whose stored comment or closed counts disagree with their rows."""
        return int(
            self.mysql.sql(
                region,
                "SELECT (SELECT COUNT(*) FROM issue i WHERE i.num_comments <> "
                "(SELECT COUNT(*) FROM comment c WHERE c.issue_id = i.id AND c.type = 0)) + "
                "(SELECT COUNT(*) FROM repository r WHERE r.num_closed_issues <> "
                "(SELECT COUNT(*) FROM issue i WHERE i.repo_id = r.id AND i.is_pull = 0 AND i.is_closed = 1));",
            )
        )

    def webhook_deliveries(self):
        return json.loads(
            self.command(
                "exec",
                f"deployment/{RECEIVER}",
                "--",
                "python",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/').read().decode())",
            )
        )

    def reference_repair(self):
        """Re-accept the stale region's unreplicated issues, comments and closes, then rebuild it as a replica.

        Works only from the two databases: anything on the stale side with no
        matching content on the primary was never replicated. Writing it again
        through the API gives it the primary's numbers and emits each event once.
        """
        primary = self.mysql.write_region()
        if primary not in REGIONS:
            primary = "west"
            self.mysql.route_writes(primary)
            self.restart_gitea()
        stale = other(primary)
        current, state = self.snapshot(primary), self.snapshot(stale)
        users = {r["id"]: r["lower_name"] for r in state["users"]}
        names = {r["id"]: users[r["owner_id"]] + "/" + r["lower_name"] for r in state["repositories"]}
        present = {(r["repo_id"], r["name"], r["content"]): r for r in current["issues"]}
        missing = [
            {"repository": names[r["repo_id"]], "title": r["name"], "body": r["content"]}
            for r in state["issues"]
            if (r["repo_id"], r["name"], r["content"]) not in present
        ]
        created = (
            {(r["repository"], r["title"], r["body"]): r for r in self.recovery_client("create", records=missing)}
            if missing
            else {}
        )
        comments = Counter((r["issue_id"], r["content"]) for r in current["comments"])
        updates = []
        for issue in state["issues"]:
            key = (issue["repo_id"], issue["name"], issue["content"])
            repository = names[issue["repo_id"]]
            target = present.get(key)
            number = target["index"] if target else created[(repository, issue["name"], issue["content"])]["number"]
            for comment in state["comments"]:
                if comment["issue_id"] != issue["id"]:
                    continue
                if target and comments[(target["id"], comment["content"])]:
                    comments[(target["id"], comment["content"])] -= 1
                    continue
                updates.append({"repository": repository, "number": number, "comment": comment["content"]})
            if issue["is_closed"] and not (target and target["is_closed"]):
                updates.append({"repository": repository, "number": number, "close": True})
        if updates:
            self.recovery_client("update", updates=updates)
        self.mysql.rebuild(stale, primary, self.secret_value("gitea-mysql", "replication-password"))
