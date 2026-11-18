"""Gitea on two regional MySQL servers, with a failover controller, a webhook relay and a hidden consumer."""

import base64
import contextlib
import json
import secrets
import subprocess
import time
from collections import Counter
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.client_runner import PythonClientRunner
from sregym.service.apps.components.mysql import MYSQL_IMAGE, REGION_LABEL, REGIONS, MySQLRegions, other
from sregym.service.apps.gitea.application import FIXTURES, GiteaApplication
from sregym.service.apps.gitea.failover import GiteaFailoverApplication
from sregym.service.apps.gitea.recovery import GiteaRecoveryApplication
from sregym.service.runtime_images import PYMYSQL_CLIENT_IMAGE

#: Shared-history issues, issues opened on the stale primary during the partition, issues opened after failover.
DIVERGENCE_COUNTS = {"small": (200, 120, 60), "large": (600, 360, 180)}
#: Other writes the stale primary acknowledged during the partition: GitHub lost about 950 writes of many kinds.
PARTITION_WRITES = {
    "small": {"comments": 200, "closes": 60, "labels": 200, "assignees": 120, "milestones": 120, "reactions": 100},
    "large": {"comments": 600, "closes": 180, "labels": 600, "assignees": 360, "milestones": 360, "reactions": 300},
}
#: Files pushed to existing repositories, and issues in a repository created, during the partition.
PARTITION_GIT = {"small": {"pushes": 20, "issues": 8}, "large": {"pushes": 60, "issues": 24}}
POST_PROMOTION_WRITES = {
    "small": {"comments": 60, "labels": 60, "reactions": 40},
    "large": {"comments": 180, "labels": 180, "reactions": 120},
}
LABELS = ("bug", "customer-reported", "needs-triage", "regression", "performance")
MILESTONES = ("2026.11", "2026.12", "2027.01")
REACTIONS = ("+1", "heart", "eyes", "rocket", "hooray", "confused")
ORCHESTRATOR = "orchestrator"
ORCHESTRATOR_STATE = "orchestrator-state"
#: One-way delay between the regions' nodes, as between GitHub's US East and US West sites.
WAN_DELAY = "10ms"
WAN_IMAGE = "nicolaka/netshoot:v0.13"
WAN = tuple(f"gitea-wan-{region}" for region in REGIONS)
RECEIVER = "gitea-webhook-receiver"
RELAY = "gitea-webhook-relay"
PYTHON_IMAGE = "python:3.12.13-alpine3.23"
DELIVER_TIMEOUT = 5
#: Most journal writes sent to the checks client in one exec.
RECOVERY_BATCH = 100
WEBHOOK_STALL_SECONDS = 30
LIVE_TRAFFIC = "gitea-users"
LIVE_TRAFFIC_REPOSITORY = "zoo-labs/zoo-utilities"
LIVE_TRAFFIC_INTERVAL = 2
#: The consumer, the users and the checks stand outside the incident, where the agent cannot see or change them.
HIDDEN = "khaos"
CHECKS = "gitea-checks"
DATABASE_CLIENT = "gitea-db-client"
HIDDEN_DEPLOYMENTS = (RECEIVER, LIVE_TRAFFIC, CHECKS, DATABASE_CLIENT, *WAN)
SHARED_SECRETS = ("gitea-mysql", "gitea-webhook", "gitea-admin")
MEMBER = "mchen"
MEMBER_SECRET = "gitea-member"

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
              'is_pull', is_pull, 'is_closed', is_closed, 'created_unix', created_unix,
              'milestone_id', milestone_id)), JSON_ARRAY())
              FROM issue),
 'comments', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('id', id, 'issue_id', issue_id, 'content', content)),
                JSON_ARRAY())
                FROM comment WHERE type = 0),
 'labels', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('issue_id', il.issue_id, 'name', l.name)), JSON_ARRAY())
              FROM issue_label il JOIN label l ON l.id = il.label_id),
 'assignees', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('issue_id', a.issue_id, 'user', u.lower_name)),
                 JSON_ARRAY())
                 FROM issue_assignees a JOIN `user` u ON u.id = a.assignee_id),
 'milestones', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('id', id, 'name', name)), JSON_ARRAY()) FROM milestone),
 'reactions', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('issue_id', r.issue_id, 'type', r.type,
                 'user', u.lower_name)), JSON_ARRAY())
                 FROM reaction r JOIN `user` u ON u.id = r.user_id WHERE r.comment_id = 0)
);
"""


def repository_names(state):
    """``owner/name`` for each repository id in a snapshot."""
    users = {r["id"]: r["lower_name"] for r in state["users"]}
    return {r["id"]: users[r["owner_id"]] + "/" + r["lower_name"] for r in state["repositories"]}


def issue_details(state):
    """Each issue's labels, assignees, reactions and milestone, by issue id."""
    milestones = {m["id"]: m["name"] for m in state.get("milestones", [])}
    details = {
        i["id"]: {
            "labels": set(),
            "assignees": set(),
            "reactions": set(),
            "milestone": milestones.get(i.get("milestone_id")),
        }
        for i in state["issues"]
    }
    for kind, value in (("labels", "name"), ("assignees", "user")):
        for row in state.get(kind, []):
            if row["issue_id"] in details:
                details[row["issue_id"]][kind].add(row[value])
    for row in state.get("reactions", []):
        if row["issue_id"] in details:
            details[row["issue_id"]]["reactions"].add((row["type"], row["user"]))
    return details


def summary(item):
    """The issue and comment a raw queued delivery describes."""
    try:
        payload = json.loads(item["body"])
    except ValueError:
        payload = {}
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    return {
        "id": item["id"],
        "delivery": item["headers"].get("X-Gitea-Delivery"),
        "event": item["headers"].get("X-Gitea-Event"),
        "action": payload.get("action"),
        "repository": (payload.get("repository") or {}).get("full_name"),
        "issue_id": issue.get("id"),
        "number": issue.get("number"),
        "title": issue.get("title"),
        "body": issue.get("body"),
        "state": issue.get("state"),
        "comment_id": comment.get("id"),
        "comment": comment.get("body"),
    }


def stale_activity(old, current, actor):
    """What the stale side acknowledged that the primary lacks, by repository path and issue content.

    Reactions can only be replayed as ``actor``, who made every reaction this environment writes.
    """
    old_names, names = repository_names(old), repository_names(current)
    present = {(names[r["repo_id"]], r["name"], r["content"]): r for r in current["issues"]}
    old_details, details = issue_details(old), issue_details(current)
    empty = {"labels": set(), "assignees": set(), "reactions": set(), "milestone": None}
    comments = Counter((r["issue_id"], r["content"]) for r in current["comments"])
    old_comments = {}
    for comment in old["comments"]:
        old_comments.setdefault(comment["issue_id"], []).append(comment["content"])
    issues, updates = [], []
    for issue in old["issues"]:
        key = (old_names[issue["repo_id"]], issue["name"], issue["content"])
        target = present.get(key)
        record = {"repository": key[0], "title": key[1], "body": key[2]}
        if target is None:
            issues.append(record)
        mine, theirs = old_details[issue["id"]], details[target["id"]] if target else empty
        for text in old_comments.get(issue["id"], []):
            if target and comments[(target["id"], text)]:
                comments[(target["id"], text)] -= 1
                continue
            updates.append({**record, "comment": text})
        if issue["is_closed"] and not (target and target["is_closed"]):
            updates.append({**record, "close": True})
        updates += [{**record, "label": n} for n in sorted(mine["labels"] - theirs["labels"])]
        updates += [{**record, "assignee": u} for u in sorted(mine["assignees"] - theirs["assignees"])]
        if mine["milestone"] and not theirs["milestone"]:
            updates.append({**record, "milestone": mine["milestone"]})
        reactions = sorted(mine["reactions"] - theirs["reactions"])
        updates += [{**record, "reaction": kind} for kind, user in reactions if user == actor]
    return {
        "repositories": sorted(set(old_names.values()) - set(names.values())),
        "issues": issues,
        "updates": updates,
    }


def undeliverable(state, deliveries):
    """Ids of deliveries the snapshot no longer matches, or that repeat an earlier delivery."""
    names = repository_names(state)
    issues = {(names[r["repo_id"]], r["index"]): r for r in state["issues"]}
    comments = {r["id"]: r for r in state["comments"]}
    stale, seen = [], set()
    for delivery in deliveries:
        issue = issues.get(((delivery["repository"] or "").lower(), delivery["number"]))
        comment = comments.get(delivery["comment_id"])
        described = (delivery["issue_id"], delivery["title"], delivery["body"])
        key = tuple(v for k, v in sorted(delivery.items()) if k not in ("id", "delivery", "state"))
        if (
            key in seen
            or issue is None
            or (issue["id"], issue["name"], issue["content"]) != described
            or (delivery["action"] == "closed" and not issue["is_closed"])
            or (
                delivery["comment_id"] is not None
                and (comment is None or (comment["issue_id"], comment["content"]) != (issue["id"], delivery["comment"]))
            )
        ):
            stale.append(delivery["id"])
        seen.add(key)
    return stale


class GiteaMySQLApplication(GiteaApplication):
    metadata_file = Path(__file__).with_name("metadata-mysql.json")
    git_inventory = GiteaRecoveryApplication.git_inventory
    repository_ids = GiteaFailoverApplication.repository_ids

    def __init__(self, tier="small", storage_class="openebs-hostpath"):
        if tier not in DIVERGENCE_COUNTS:
            raise ValueError(f"Unknown tier {tier!r}; choose from {tuple(DIVERGENCE_COUNTS)}")
        super().__init__("single", storage_class)
        self.scale_tier = tier
        self.divergence_counts = DIVERGENCE_COUNTS[tier]
        self.partition_writes = PARTITION_WRITES[tier]
        self.partition_git = PARTITION_GIT[tier]
        self.post_promotion_writes = POST_PROMOTION_WRITES[tier]
        self.description = self.get_app_json()["Desc"]

    def get_app_json(self):
        return Application.get_app_json(self)

    @property
    def mysql(self):
        return MySQLRegions(
            self.command, "gitea-db", "gitea", self.storage_class, "gitea-mysql", client=(self.hidden, DATABASE_CLIENT)
        )

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
            spec = document["spec"]["template"]["spec"] if document["kind"] == "Deployment" else document["spec"]
            spec["nodeSelector"] = {REGION_LABEL: "east"}
        container = next(d for d in documents if d["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e for e in container["env"] if e["name"] != "GITEA__database__SSL_MODE"}
        for name, value in {
            "GITEA__database__DB_TYPE": "mysql",
            "GITEA__database__HOST": f"{self.mysql.write_service}:3306",
            "GITEA__webhook__ALLOWED_HOST_LIST": RELAY,
            "GITEA__webhook__DELIVER_TIMEOUT": str(DELIVER_TIMEOUT),
        }.items():
            env[name] = {"name": name, "value": value}
        container["env"] = list(env.values())
        return [*self.mysql.render("gitea-database"), *documents]

    def receiver_documents(self):
        secret = {"secretKeyRef": {"name": "gitea-webhook", "key": "secret"}}
        return self.python_pod(
            RECEIVER, "west", "webhook_receiver.py", [{"name": "WEBHOOK_SECRET", "valueFrom": secret}]
        )

    def relay_documents(self, target):
        return self.python_pod(
            RELAY,
            "east",
            "webhook_relay.py",
            [{"name": "TARGET", "value": target}, {"name": "TIMEOUT", "value": str(DELIVER_TIMEOUT)}],
        )

    @staticmethod
    def python_pod(name, region, script, env, volumes=(), mounts=(), service=True):
        """A one-replica Python helper running ``script``, served on port 80 when ``service`` is set."""
        labels = {"app": name, "region": region}
        container = {
            "name": "main",
            "image": PYTHON_IMAGE,
            "command": ["python", "-u", "/app/main.py"],
            "env": env,
            "volumeMounts": [{"name": "app", "mountPath": "/app"}, *mounts],
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
        }
        documents = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name},
                "data": {"main.py": Path(__file__).with_name(script).read_text()},
            }
        ]
        if service:
            container["ports"] = [{"containerPort": 8080}]
            container["readinessProbe"] = {"tcpSocket": {"port": 8080}, "periodSeconds": 5}
            documents.append(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": name},
                    "spec": {"selector": labels, "ports": [{"port": 80, "targetPort": 8080}]},
                }
            )
        documents.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "nodeSelector": {REGION_LABEL: region},
                            "volumes": [{"name": "app", "configMap": {"name": name}}, *volumes],
                            "containers": [container],
                        },
                    },
                },
            }
        )
        return documents

    @staticmethod
    def idle_pod(name, image, env=(), volumes=(), mounts=()):
        """A one-replica pod that only waits to be exec'd into."""
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        # sleep ignores SIGTERM as PID 1, so do not wait it out.
                        "terminationGracePeriodSeconds": 1,
                        "volumes": list(volumes),
                        "containers": [
                            {
                                "name": "main",
                                "image": image,
                                "command": ["sleep", "infinity"],
                                "env": list(env),
                                "volumeMounts": list(mounts),
                            }
                        ],
                    },
                },
            },
        }

    def label_regions(self):
        """Put the regions on two separate worker nodes, so traffic between them crosses a link."""
        nodes = json.loads(self.command("get", "nodes", "-o", "json"))["items"]
        workers = sorted(
            n["metadata"]["name"]
            for n in nodes
            if "node-role.kubernetes.io/control-plane" not in n["metadata"].get("labels", {})
        )
        if len(workers) < len(REGIONS):
            raise RuntimeError(f"The regions need {len(REGIONS)} worker nodes; the cluster has {len(workers)}")
        # Never replace a region the cluster's own provider set; only this environment's values are ours.
        foreign = {
            n["metadata"]["name"]: value
            for n in nodes
            if (value := n["metadata"].get("labels", {}).get(REGION_LABEL)) and value not in REGIONS
        }
        if foreign:
            raise RuntimeError(f"Nodes already carry {REGION_LABEL}: {foreign}")
        self.unlabel_regions()
        for region, node in zip(REGIONS, workers, strict=False):
            self.command("label", "node", node, f"{REGION_LABEL}={region}")

    def unlabel_regions(self):
        for node in json.loads(self.command("get", "nodes", "-o", "json"))["items"]:
            if node["metadata"].get("labels", {}).get(REGION_LABEL) in REGIONS:
                self.command("label", "node", node["metadata"]["name"], f"{REGION_LABEL}-")

    def wan_documents(self):
        """A delay on traffic between the regions' nodes, applied in each node's network namespace."""
        nodes = {
            n["metadata"]["labels"][REGION_LABEL]: n
            for n in json.loads(self.command("get", "nodes", "-o", "json"))["items"]
            if n["metadata"].get("labels", {}).get(REGION_LABEL) in REGIONS
        }
        blocks = json.loads(self.command("get", "blockaffinities.crd.projectcalico.org", "-o", "json"))["items"]
        peers = {}
        for region, node in nodes.items():
            address = next(a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP")
            cidrs = [b["spec"]["cidr"] for b in blocks if b["spec"]["node"] == node["metadata"]["name"]]
            peers[region] = [f"{address}/32", *cidrs]
        documents = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "gitea-wan"},
                "data": {"wan.sh": Path(__file__).with_name("wan.sh").read_text()},
            }
        ]
        for region, name in zip(REGIONS, WAN, strict=True):
            container = {
                "name": "main",
                "image": WAN_IMAGE,
                "command": ["sh", "/app/wan.sh"],
                "env": [
                    {"name": "DELAY", "value": WAN_DELAY},
                    {"name": "PEERS", "value": " ".join(peers[other(region)])},
                ],
                "securityContext": {"privileged": True},
                "volumeMounts": [{"name": "app", "mountPath": "/app"}],
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
            }
            spec = {
                "automountServiceAccountToken": False,
                "hostNetwork": True,
                "nodeSelector": {REGION_LABEL: region},
                "terminationGracePeriodSeconds": 10,
                "volumes": [{"name": "app", "configMap": {"name": "gitea-wan"}}],
                "containers": [container],
            }
            documents.append(
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": name},
                    "spec": {
                        "replicas": 1,
                        # A new pod must not start before the old one's exit removes the qdisc it would install.
                        "strategy": {"type": "Recreate"},
                        "selector": {"matchLabels": {"app": name}},
                        "template": {"metadata": {"labels": {"app": name}}, "spec": spec},
                    },
                }
            )
        return documents

    def install_wan(self):
        self.apply_hidden(self.wan_documents())
        for name in WAN:
            self.hidden("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=320)

    def remove_wan(self):
        # The nodes outlive this namespace; take the delay off them even if a pod's own cleanup did not run.
        for name in WAN:
            with contextlib.suppress(subprocess.CalledProcessError, subprocess.TimeoutExpired):
                self.hidden(
                    "exec",
                    f"deployment/{name}",
                    "--",
                    "sh",
                    "-c",
                    'tc qdisc del dev "$(cat /tmp/wan-dev)" root',
                    timeout=30,
                )

    def orchestrator_documents(self, state):
        secret = {"name": "gitea-mysql"}
        instances = {self.mysql.pod(r): {"host": self.mysql.service(r), "region": r} for r in REGIONS}
        # No region label: the partition between the regions does not cut Orchestrator off from either.
        labels = {"app": ORCHESTRATOR}
        rule = {"apiGroups": [""], "verbs": ["get", "patch"]}
        env = {
            "CLUSTER": "gitea",
            "INSTANCES": json.dumps(instances),
            "ROUTE": self.mysql.write_service,
            "STATE": ORCHESTRATOR_STATE,
            "STATE_NAMESPACE": HIDDEN,
            "POLL_SECONDS": "5",
            "DEAD_AFTER_POLLS": "3",
            "RECOVERY_PERIOD_BLOCK_SECONDS": "3600",
        }
        container = {
            "name": "main",
            "image": PYMYSQL_CLIENT_IMAGE,
            "command": ["python", "-u", "/app/main.py"],
            "env": [
                *({"name": k, "value": v} for k, v in env.items()),
                {"name": "MYSQL_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": {**secret, "key": "root-password"}}},
                {
                    "name": "REPLICATION_PASSWORD",
                    "valueFrom": {"secretKeyRef": {**secret, "key": "replication-password"}},
                },
            ],
            "ports": [{"containerPort": 3000}],
            "readinessProbe": {"httpGet": {"path": "/api/health", "port": 3000}, "periodSeconds": 5},
            "volumeMounts": [{"name": "app", "mountPath": "/app"}],
            "resources": {"requests": {"cpu": "10m", "memory": "48Mi"}, "limits": {"memory": "192Mi"}},
        }
        return [
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": ORCHESTRATOR}},
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": ORCHESTRATOR},
                "rules": [{**rule, "resources": ["services"], "resourceNames": [self.mysql.write_service]}],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": ORCHESTRATOR},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": ORCHESTRATOR},
                "subjects": [{"kind": "ServiceAccount", "name": ORCHESTRATOR, "namespace": self.namespace}],
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": ORCHESTRATOR},
                "data": {"main.py": Path(__file__).with_name("orchestrator.py").read_text()},
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": ORCHESTRATOR},
                "spec": {"selector": labels, "ports": [{"port": 3000, "targetPort": 3000}]},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": ORCHESTRATOR},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "serviceAccountName": ORCHESTRATOR,
                            "nodeSelector": {REGION_LABEL: "west"},
                            "volumes": [{"name": "app", "configMap": {"name": ORCHESTRATOR}}],
                            "containers": [container],
                        },
                    },
                },
            },
        ]

    def start_orchestrator(self):
        """Orchestrator watching the east primary, free to recover it automatically."""
        state = {"master": self.mysql.pod("east"), "recoveries_enabled": True, "last_recovery": 0, "audit": []}
        # Orchestrator's backend lives outside the incident namespace, as its own database would.
        subject = {"kind": "ServiceAccount", "name": ORCHESTRATOR, "namespace": self.namespace}
        self.apply_hidden(
            [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": ORCHESTRATOR_STATE},
                    "data": {"state.json": json.dumps(state)},
                },
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "Role",
                    "metadata": {"name": ORCHESTRATOR},
                    "rules": [
                        {
                            "apiGroups": [""],
                            "verbs": ["get", "patch"],
                            "resources": ["configmaps"],
                            "resourceNames": [ORCHESTRATOR_STATE],
                        }
                    ],
                },
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {"name": ORCHESTRATOR},
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": ORCHESTRATOR},
                    "subjects": [subject],
                },
            ]
        )
        self.apply(self.orchestrator_documents(state))
        self.command("rollout", "status", f"deployment/{ORCHESTRATOR}", "--timeout=300s", timeout=320)
        # From here Orchestrator owns the route; a stale record of the original manifest would name the old primary.
        self.command(
            "annotate", "service", self.mysql.write_service, "kubectl.kubernetes.io/last-applied-configuration-"
        )

    def wait_for_orchestrator_master(self, region, timeout=180):
        """Wait until Orchestrator has recovered to ``region``; return when it finished."""
        deadline = time.monotonic() + timeout
        while (state := self.orchestrator_state())["master"] != self.mysql.pod(region):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Orchestrator did not promote {region}: {state}")
            time.sleep(3)
        return state["last_recovery"]

    def orchestrator_state(self):
        """What Orchestrator records as the master and whether it may recover, read from its backend."""
        configmap = json.loads(self.hidden("get", "configmap", ORCHESTRATOR_STATE, "-o", "json"))
        return json.loads(configmap["data"]["state.json"])

    def orchestrator(self, command):
        return json.loads(self.local_http(ORCHESTRATOR, "/api/" + command, "POST", port=3000))

    def checks_documents(self):
        root = {"secretKeyRef": {"name": "gitea-mysql", "key": "root-password"}}
        return [
            self.idle_pod(
                CHECKS,
                "python:3.12-alpine",
                env=[{"name": "GITEA_URL", "value": f"http://gitea.{self.namespace}.svc:3000"}],
                volumes=[{"name": "credentials", "secret": {"secretName": "gitea-admin"}}],
                mounts=[{"name": "credentials", "mountPath": "/credentials", "readOnly": True}],
            ),
            self.idle_pod(DATABASE_CLIENT, MYSQL_IMAGE, env=[{"name": "MYSQL_PWD", "valueFrom": root}]),
        ]

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
        self.label_regions()
        self.ensure_secret(
            # MySQL limits replication passwords to 32 characters.
            "gitea-mysql",
            {"root-password": secrets.token_hex(24), "replication-password": secrets.token_hex(16)},
        )
        self.ensure_secret("gitea-webhook", {"secret": secrets.token_hex(24)})
        self.prepare_hidden()
        database = [
            d
            for d in documents
            if d["kind"] in ("Service", "StatefulSet") and d["metadata"].get("labels", {}).get("app") == "gitea-db"
        ]
        self.apply(database)
        self.mysql.wait_ready()
        self.mysql.initialize(self.secret_value("gitea-mysql", "replication-password"))
        return database

    def hidden(self, *args, **kwargs):
        return self.command("-n", HIDDEN, *args, **kwargs)

    def kubectl_for(self, name):
        return self.hidden if name in HIDDEN_DEPLOYMENTS else self.command

    def apply_hidden(self, documents):
        self.hidden("apply", "-f", "-", input_text=json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}))

    def remove_hidden(self):
        self.remove_wan()
        objects = [
            *(f"{kind}/{name}" for name in HIDDEN_DEPLOYMENTS for kind in ("deployment", "service", "configmap")),
            *(f"secret/{name}" for name in (*SHARED_SECRETS, MEMBER_SECRET)),
            "configmap/gitea-wan",
            f"configmap/{ORCHESTRATOR_STATE}",
            f"role/{ORCHESTRATOR}",
            f"rolebinding/{ORCHESTRATOR}",
        ]
        with contextlib.suppress(subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # Foreground, so no old pod is left for an exec to land on once the new ones are applied.
            self.hidden("delete", *objects, "--ignore-not-found", "--cascade=foreground", timeout=200)

    def prepare_hidden(self):
        """The checks' clients and the secrets the hidden pods use, before anything is checked."""
        if not self.command("get", "namespace", HIDDEN, "--ignore-not-found", "-o", "name").strip():
            self.command("create", "namespace", HIDDEN)
        self.remove_hidden()
        for name in SHARED_SECRETS:
            secret = json.loads(self.command("get", "secret", name, "-o", "json"))
            self.apply_hidden(
                [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "data": secret["data"]}]
            )
        member = {"username": MEMBER, "password": secrets.token_hex(16)}
        self.apply_hidden(
            [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": MEMBER_SECRET}, "stringData": member}]
        )
        self.apply_hidden(self.checks_documents())
        for name in (CHECKS, DATABASE_CLIENT):
            self.hidden("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=320)

    def run_client(self, mode, **arguments):
        return PythonClientRunner(self.hidden, f"deployment/{CHECKS}").run(
            [Path(__file__).with_name("workflow.py")],
            "run(" + repr(mode) + ", **json.loads(" + repr(json.dumps(arguments)) + "))",
        )

    def recovery_client(self, mode, **arguments):
        # Long batches go in order, in pieces that each finish well inside the exec timeout.
        for key in ("records", "updates"):
            if len(arguments.get(key) or ()) > RECOVERY_BATCH:
                items = arguments[key]
                return [
                    result
                    for start in range(0, len(items), RECOVERY_BATCH)
                    for result in self.recovery_client(
                        mode, **{**arguments, key: items[start : start + RECOVERY_BATCH]}
                    )
                ]
        directory = Path(__file__).parent
        source = (directory / "workflow.py").read_text() + "\n" + (directory / "recovery_workflow.py").read_text()
        source += "\nrun_recovery(api, " + repr(mode) + ", **json.loads(" + repr(json.dumps(arguments)) + "))\n"
        output = self.hidden("exec", "-i", f"deployment/{CHECKS}", "--", "python", "-", input_text=source, timeout=300)
        return json.loads(output)

    def deploy(self):
        super().deploy()
        member = json.loads(self.hidden("get", "secret", MEMBER_SECRET, "-o", "json"))["data"]
        self.recovery_client(
            "member",
            username=MEMBER,
            password=base64.b64decode(member["password"]).decode(),
            repository=LIVE_TRAFFIC_REPOSITORY,
        )
        self.apply_hidden(self.receiver_documents())
        self.hidden("rollout", "status", f"deployment/{RECEIVER}", "--timeout=300s", timeout=320)
        self.apply(self.relay_documents(f"http://{self.receiver_address()}/"))
        self.command("rollout", "status", f"deployment/{RELAY}", "--timeout=300s", timeout=320)
        repositories = [
            r["owner"] + "/" + r["name"]
            for r in json.loads((FIXTURES / "import-data.json").read_text())["repositories"]
        ]
        self.recovery_client(
            "webhooks",
            repositories=repositories,
            url=f"http://{RELAY}/",
            secret=self.secret_value("gitea-webhook", "secret"),
        )
        self.recovery_client("catalog", repositories=repositories, labels=LABELS, milestones=MILESTONES)
        self.start_orchestrator()

    def live_traffic_documents(self):
        return self.python_pod(
            LIVE_TRAFFIC,
            "east",
            "live_traffic.py",
            [
                {"name": "GITEA", "value": f"http://gitea.{self.namespace}.svc:3000"},
                {"name": "REPOSITORY", "value": LIVE_TRAFFIC_REPOSITORY},
                {"name": "INTERVAL", "value": str(LIVE_TRAFFIC_INTERVAL)},
            ],
            volumes=[{"name": "credentials", "secret": {"secretName": MEMBER_SECRET}}],
            mounts=[{"name": "credentials", "mountPath": "/credentials", "readOnly": True}],
            service=False,
        )

    def start_live_traffic(self):
        self.apply_hidden(self.live_traffic_documents())
        self.hidden("rollout", "status", f"deployment/{LIVE_TRAFFIC}", "--timeout=300s", timeout=320)

    def pod_identities(self, name):
        pods = json.loads(self.kubectl_for(name)("get", "pods", "-l", f"app={name}", "-o", "json"))["items"]
        return [
            (p["metadata"]["uid"], sum(c["restartCount"] for c in p["status"].get("containerStatuses", [])))
            for p in pods
        ]

    def local_http(self, deployment, path="/", method="GET", port=8080):
        """Call a helper pod's own HTTP server from inside the pod."""
        script = (
            "import sys, urllib.request; "
            "print(urllib.request.urlopen(urllib.request.Request(sys.argv[1], method=sys.argv[2])).read().decode())"
        )
        # Isolated, so nothing planted in the pod's Python path runs in place of the script.
        python = ("python", "-I", "-S", "-c", script)
        return self.kubectl_for(deployment)(
            "exec", f"deployment/{deployment}", "--", *python, f"http://127.0.0.1:{port}" + path, method
        )

    def control(self, deployment, action=None):
        """POST an action to a helper pod, or read its state."""
        if action:
            self.local_http(deployment, f"/{action}", "POST")
            return None
        return json.loads(self.local_http(deployment))

    def live_traffic(self, action=None):
        return self.control(LIVE_TRAFFIC, action)

    def cleanup(self):
        self.remove_hidden()
        super().cleanup()
        self.unlabel_regions()

    def snapshot(self, region=None):
        state = json.loads(self.mysql.sql(region or self.mysql.write_region(), SNAPSHOT_SQL))
        state["users"].sort(key=lambda r: r["id"])
        state["repositories"].sort(key=lambda r: r["id"])
        state["issues"] = sorted(
            (r for r in state["issues"] if not r["is_pull"]), key=lambda r: (r["repo_id"], r["index"])
        )
        state["comments"].sort(key=lambda r: r["id"])
        return state

    def admin_username(self):
        secret = json.loads(self.command("get", "secret", "gitea-admin", "-o", "json"))
        return base64.b64decode(secret["data"]["username"]).decode().lower()

    def stale_branches(self):
        """Repositories whose recorded default-branch head differs from the Git ref Gitea serves from disk."""
        rows = self.mysql.sql(
            self.mysql.write_region(),
            "SELECT CONCAT(u.lower_name, '/', r.lower_name), r.default_branch, COALESCE(b.commit_id, '-') "
            "FROM repository r JOIN `user` u ON u.id = r.owner_id LEFT JOIN branch b "
            "ON b.repo_id = r.id AND b.name = r.default_branch AND b.is_deleted = 0;",
        ).splitlines()
        recorded = {name: (branch, commit) for name, branch, commit in (row.split("\t") for row in rows)}
        script = (
            'for r in "$@"; do printf "%s %s\\n" "${r%%:*}" '
            '"$(git --git-dir "/data/git/repositories/${r%%:*}.git" rev-parse "refs/heads/${r#*:}" 2>/dev/null '
            '|| echo -)"; done'
        )
        heads = self.command(
            "exec",
            "deployment/gitea",
            "--",
            "su-exec",
            "git",
            "sh",
            "-c",
            script,
            "heads",
            *(f"{name}:{branch}" for name, (branch, _) in sorted(recorded.items())),
            timeout=120,
        )
        found = dict(line.split(" ", 1) for line in heads.splitlines() if " " in line)
        return sorted(name for name, (_, commit) in recorded.items() if found.get(name, "-") != commit)

    def sync_branches(self, timeout=300):
        """Have Gitea re-read every repository's branches from Git, and wait until it has."""
        self.recovery_client("sync-branches")
        deadline = time.monotonic() + timeout
        while stale := self.stale_branches():
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Branch records still differ from Git: {stale}")
            time.sleep(5)

    def from_client(self, script, *args):
        """Run ``script`` in the region-east client pod, as Gitea would; False if it fails."""
        try:
            self.command("exec", "application-client", "--", "python", "-c", script, *args, timeout=30)
        except subprocess.CalledProcessError:
            return False
        return True

    def client_fetch(self, url):
        script = "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3).read()"
        return self.from_client(script, url)

    def receiver_address(self):
        return self.hidden("get", "service", RECEIVER, "-o", "jsonpath={.spec.clusterIP}").strip()

    def receiver_reachable(self):
        script = "import socket, sys; socket.create_connection((sys.argv[1], 80), timeout=3).close()"
        return self.from_client(script, self.receiver_address())

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

    def delivery_ids(self, region):
        """The X-Gitea-Delivery id of every webhook delivery Gitea recorded."""
        return set(self.mysql.sql(region, "SELECT uuid FROM hook_task;").split())

    def opened_issue_ids(self, region):
        """Issues whose creation event Gitea queued for delivery."""
        rows = self.mysql.sql(
            region,
            "SELECT CAST(JSON_EXTRACT(payload_content, '$.issue.id') AS UNSIGNED) FROM hook_task "
            "WHERE event_type = 'issues' AND JSON_VALID(payload_content) "
            "AND JSON_UNQUOTE(JSON_EXTRACT(payload_content, '$.action')) = 'opened';",
        )
        return {int(row) for row in rows.split()}

    def stuck_webhooks(self, region, age=WEBHOOK_STALL_SECONDS):
        """Deliveries Gitea saved but never attempted, such as one it could not mark while the primary moved."""
        rows = self.mysql.sql(
            region,
            "SELECT h.id, h.uuid, h.hook_id, CONCAT(u.lower_name, '/', r.lower_name) FROM hook_task h "
            "JOIN webhook w ON w.id = h.hook_id JOIN repository r ON r.id = w.repo_id "
            "JOIN `user` u ON u.id = r.owner_id "
            f"WHERE h.is_delivered = 0 AND h.delivered < (UNIX_TIMESTAMP() - {int(age)}) * 1000000000 ORDER BY h.id;",
        ).splitlines()
        return [dict(zip(("id", "uuid", "hook_id", "repository"), row.split("	"), strict=True)) for row in rows]

    def redeliver_stuck_webhooks(self, region):
        """Mark each stuck delivery attempted, so a later restart cannot send it too, then replay it."""
        if not (stuck := self.stuck_webhooks(region)):
            return
        ids = ", ".join(str(int(task["id"])) for task in stuck)
        self.mysql.sql(region, f"UPDATE hook_task SET is_delivered = 1 WHERE id IN ({ids});")
        self.recovery_client(
            "replay-webhooks", tasks=[{k: t[k] for k in ("repository", "hook_id", "uuid")} for t in stuck]
        )

    def wait_for_webhook_attempts(self, region):
        # Partitioned deliveries each wait out DELIVER_TIMEOUT before failing. A task saved by a
        # request that was then cancelled is only queued when Gitea restarts, so stop once nothing moves.
        pending = self.undelivered_webhooks(region)
        deadline = time.monotonic() + max(300, (DELIVER_TIMEOUT + 1) * pending + 120)
        moved = time.monotonic()
        while pending and time.monotonic() - moved < WEBHOOK_STALL_SECONDS:
            if time.monotonic() >= deadline:
                raise RuntimeError("Gitea did not attempt every queued webhook delivery")
            time.sleep(3)
            if (remaining := self.undelivered_webhooks(region)) != pending:
                pending, moved = remaining, time.monotonic()
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
                "SELECT (SELECT COUNT(*) FROM issue i WHERE i.is_pull = 0 AND i.num_comments <> "
                "(SELECT COUNT(*) FROM comment c WHERE c.issue_id = i.id AND c.type = 0)) + "
                "(SELECT COUNT(*) FROM repository r WHERE r.num_closed_issues <> "
                "(SELECT COUNT(*) FROM issue i WHERE i.repo_id = r.id AND i.is_pull = 0 AND i.is_closed = 1)) + "
                "(SELECT COUNT(*) FROM label l WHERE l.num_issues <> "
                "(SELECT COUNT(*) FROM issue_label il WHERE il.label_id = l.id)) + "
                "(SELECT COUNT(*) FROM milestone m WHERE m.num_issues <> "
                "(SELECT COUNT(*) FROM issue i WHERE i.milestone_id = m.id) OR m.num_closed_issues <> "
                "(SELECT COUNT(*) FROM issue i WHERE i.milestone_id = m.id AND i.is_closed = 1));",
            )
        )

    def webhook_deliveries(self):
        return json.loads(self.local_http(RECEIVER))

    def relay(self, action=None):
        return self.control(RELAY, action)

    def queued_events(self):
        return [summary(item) for item in self.relay()["queued"]]

    def drop_queued(self, ids):
        self.local_http(RELAY, "/queue/" + ",".join(str(i) for i in ids), "DELETE")

    def wait_for_relay(self, timeout=120):
        """The relay's state once it has forwarded everything it can."""
        deadline = time.monotonic() + timeout
        while (state := self.relay())["queued"] and not state["paused"] and time.monotonic() < deadline:
            time.sleep(2)
        return state

    def reference_repair(self):
        """Fail back to the home region, then re-accept the stale side's unreplicated activity.

        Works only from the two databases, the relay and Orchestrator, in the order GitHub
        recovered: hold webhook delivery and automated failover, save what only the stale side
        has, rebuild it from the primary, move the primary back beside Gitea, then write the
        saved activity again through the API so it gets the primary's numbers and emits each
        event once.
        """
        primary = self.mysql.write_region()
        if primary not in REGIONS:
            primary = "west"
            self.mysql.route_writes(primary)
            self.restart_gitea()
        stale = other(primary)
        self.orchestrator("disable-global-recoveries")
        # Hold delivery while sorting the backlog; a forward already in flight times out first.
        self.relay("pause")
        time.sleep(DELIVER_TIMEOUT + 1)
        current, old = self.snapshot(primary), self.snapshot(stale)
        if doomed := undeliverable(current, self.queued_events()):
            self.drop_queued(doomed)
        plan = stale_activity(old, current, self.admin_username())
        self.mysql.rebuild(stale, primary, self.secret_value("gitea-mysql", "replication-password"))
        if stale == "east":
            self.orchestrator(f"graceful-master-takeover-auto/gitea/{self.mysql.pod(stale)}/3306")
            self.mysql.sql(stale, "SET PERSIST read_only = OFF;\nSET PERSIST super_read_only = OFF;")
            self.mysql.sql(primary, "SET PERSIST super_read_only = ON;")
            # Gitea's pooled connections to the old primary expire within its CONN_MAX_LIFETIME.
            time.sleep(10)
        self.orchestrator("enable-global-recoveries")
        if plan["repositories"]:
            self.recovery_client("adopt", repositories=plan["repositories"])
        created = self.recovery_client("create", records=plan["issues"]) if plan["issues"] else []
        current = self.snapshot()
        names = repository_names(current)
        numbers = {(names[r["repo_id"]], r["name"], r["content"]): r["index"] for r in current["issues"]}
        numbers.update({(r["repository"], r["title"], r["body"]): r["number"] for r in created})
        updates = [{**u, "number": numbers[(u["repository"], u["title"], u["body"])]} for u in plan["updates"]]
        if updates:
            self.recovery_client("update", updates=updates)
        self.sync_branches()
        # A delivery Gitea tried to mark while the old primary was read-only was never sent.
        self.redeliver_stuck_webhooks(self.mysql.write_region())
        self.relay("resume")
