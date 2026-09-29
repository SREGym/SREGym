"""A real Gitea application with persistent repositories and PostgreSQL replicas."""

import base64
import json
import secrets
import subprocess
import time
from pathlib import Path

import yaml

from sregym.generators.workload.wrk2 import Wrk2, Wrk2WorkloadManager
from sregym.service.apps.base import Application
from sregym.service.apps.helpers import get_frontend_url
from sregym.service.apps.persistent import cleanup_persistent_namespace
from sregym.service.kubectl import KubeCtl

GITEA_IMAGE = "gitea/gitea:1.27.3"
POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:16.14-system-trixie"
OPERATOR_IMAGE = "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1"
TIERS = {"single": 1, "replicated": 3}
FIXTURES = Path(__file__).with_name("fixtures") / "gitea-zoo"


class Gitea(Application):
    def __init__(self, tier="replicated", storage_class="standard"):
        if tier not in TIERS:
            raise ValueError(f"Unknown Gitea tier {tier!r}; choose from {tuple(TIERS)}")
        super().__init__(Path(__file__).parents[1] / "metadata/gitea.json")
        self.scale_tier = tier
        self.members = TIERS[tier]
        self.storage_class = storage_class
        self.load_app_json()
        self.kubectl = KubeCtl()
        self.frontend_service = "gitea"
        self.frontend_port = 3000
        self.helm_deploy = False
        self.mount_failure_scripts = False

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]

    def get_app_json(self):
        metadata = super().get_app_json()
        if hasattr(self, "members"):
            metadata["Desc"] += f" Tier {self.scale_tier}: {self.members} PostgreSQL members and one Gitea server."
        return metadata

    def command(self, *args, input_text=None, timeout=120):
        return subprocess.run(
            ["kubectl", "-n", self.namespace, *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout

    @property
    def expected_volume_count(self):
        return self.members + 1

    def apply(self, documents):
        self.command("apply", "-f", "-", input_text=yaml.safe_dump_all(documents, sort_keys=False))

    def render(self):
        labels = {"app": "gitea"}
        postgres = {
            "parameters": {"shared_buffers": "64MB", "max_connections": "80", "max_wal_size": "256MB"},
        }
        if self.members > 1:
            postgres["synchronous"] = {"method": "any", "number": 1, "dataDurability": "required"}
        env = {
            "GITEA__database__DB_TYPE": "postgres",
            "GITEA__database__HOST": "gitea-db-rw:5432",
            "GITEA__database__NAME": "gitea",
            "GITEA__database__USER": "gitea",
            "GITEA__database__SSL_MODE": "require",
            "GITEA__security__INSTALL_LOCK": "true",
            # Preserve the imported demo account passwords (shortest: six characters).
            "GITEA__security__MIN_PASSWORD_LENGTH": "6",
            "GITEA__server__ROOT_URL": "http://gitea:3000/",
            "GITEA__server__DOMAIN": "gitea",
            "GITEA__server__DISABLE_SSH": "true",
            "GITEA__service__DISABLE_REGISTRATION": "true",
            "GITEA__mailer__ENABLED": "false",
            "GITEA__server__OFFLINE_MODE": "true",
            "GITEA__metrics__ENABLED": "true",
            "GITEA__log__LEVEL": "Info",
        }
        return [
            {
                "apiVersion": "postgresql.cnpg.io/v1",
                "kind": "Cluster",
                "metadata": {"name": "gitea-db"},
                "spec": {
                    "instances": self.members,
                    "imageName": POSTGRES_IMAGE,
                    "bootstrap": {
                        "initdb": {"database": "gitea", "owner": "gitea", "secret": {"name": "gitea-database"}}
                    },
                    "storage": {"size": "2Gi", "storageClass": self.storage_class},
                    "postgresql": postgres,
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "768Mi"},
                    },
                    "affinity": {"enablePodAntiAffinity": True, "podAntiAffinityType": "preferred"},
                },
            },
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "gitea-repositories"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": self.storage_class,
                    "resources": {"requests": {"storage": "2Gi"}},
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "gitea"},
                "spec": {"selector": labels, "ports": [{"port": 3000, "targetPort": 3000}]},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "gitea"},
                "spec": {
                    "replicas": 1,
                    "strategy": {"type": "Recreate"},
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "gitea-repositories"}}],
                            "containers": [
                                {
                                    "name": "gitea",
                                    "image": GITEA_IMAGE,
                                    "ports": [{"containerPort": 3000}],
                                    "env": [{"name": key, "value": value} for key, value in env.items()]
                                    + [
                                        {
                                            "name": "GITEA__database__PASSWD",
                                            "valueFrom": {
                                                "secretKeyRef": {"name": "gitea-database", "key": "password"}
                                            },
                                        }
                                    ],
                                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "256Mi"},
                                        "limits": {"memory": "1Gi"},
                                    },
                                    "readinessProbe": {
                                        "httpGet": {"path": "/api/healthz", "port": 3000},
                                        "periodSeconds": 5,
                                    },
                                    "startupProbe": {
                                        "httpGet": {"path": "/api/healthz", "port": 3000},
                                        "periodSeconds": 5,
                                        "failureThreshold": 180,
                                    },
                                }
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "application-client"},
                "spec": {
                    "automountServiceAccountToken": False,
                    "volumes": [{"name": "credentials", "secret": {"secretName": "gitea-admin"}}],
                    "containers": [
                        {
                            "name": "client",
                            "image": "python:3.12-alpine",
                            "command": ["sleep", "infinity"],
                            "volumeMounts": [{"name": "credentials", "mountPath": "/credentials", "readOnly": True}],
                            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
                        }
                    ],
                },
            },
        ]

    def deploy(self):
        operator = json.loads(
            self.command("get", "deployment", "cnpg-controller-manager", "-n", "cnpg-system", "-o", "json")
        )
        if not any(c["image"] == OPERATOR_IMAGE for c in operator["spec"]["template"]["spec"]["containers"]):
            raise RuntimeError(
                "Install the pinned CloudNativePG operator before the benchmark: python scripts/install_cnpg.py"
            )
        self.create_namespace()
        self.command("get", "storageclass", self.storage_class)
        for name, username in (("gitea-database", "gitea"), ("gitea-admin", "benchmark")):
            if not self.command("get", "secret", name, "--ignore-not-found", "-o", "name").strip():
                self.apply(
                    [
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {"name": name},
                            "type": "kubernetes.io/basic-auth",
                            "stringData": {"username": username, "password": secrets.token_hex(24)},
                        }
                    ]
                )
        documents = self.render()
        self.apply([d for d in documents if d["kind"] == "Cluster"])
        self.wait_database()
        self.apply([d for d in documents if d["kind"] != "Cluster"])
        self.command("rollout", "status", "deployment/gitea", "--timeout=900s", timeout=920)
        self.command("wait", "--for=condition=Ready", "pod/application-client", "--timeout=300s", timeout=320)
        if not self.command("get", "configmap", "business-data-seeded", "--ignore-not-found", "-o", "name").strip():
            secret = json.loads(self.command("get", "secret", "gitea-admin", "-o", "json"))
            password = base64.b64decode(secret["data"]["password"]).decode()
            self.command(
                "exec",
                "deployment/gitea",
                "--",
                "su-exec",
                "git",
                "gitea",
                "admin",
                "user",
                "create",
                "--username",
                "benchmark",
                "--password",
                password,
                "--email",
                "benchmark@sregym.local",
                "--admin",
                "--must-change-password=false",
                "--config",
                "/data/gitea/conf/app.ini",
            )
            self.run_client("seed", fixture=json.loads((FIXTURES / "import-data.json").read_text()))
            self.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "business-data-seeded"},
                        "data": {"upstream": "bgrins/the_zoo@862c02dd73f2101a4b24f168d0fb7fed387dc5f4"},
                    }
                ]
            )
        self.check_workflow()

    def cluster(self):
        return json.loads(self.command("get", "cluster.postgresql.cnpg.io", "gitea-db", "-o", "json"))

    def database_pods(self):
        pods = json.loads(self.command("get", "pods", "-l", "cnpg.io/cluster=gitea-db", "-o", "json"))["items"]
        return [
            p
            for p in pods
            if any(c["name"] == "postgres" for c in p["spec"]["containers"])
            and not p["metadata"].get("deletionTimestamp")
        ]

    def sql(self, statement, pod=None):
        pod = pod or self.cluster()["status"]["currentPrimary"]
        return self.command(
            "exec",
            "-i",
            pod,
            "-c",
            "postgres",
            "--",
            "psql",
            "-U",
            "postgres",
            "-d",
            "gitea",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            input_text=statement,
            timeout=45,
        ).strip()

    def wait_database(self, timeout=600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cluster = self.cluster()
            status = cluster.get("status", {})
            if (
                cluster["spec"]["instances"] == self.members
                and status.get("readyInstances") == self.members
                and status.get("currentPrimary") == status.get("targetPrimary")
                and status.get("currentPrimary")
            ):
                pods = self.database_pods()
                if len(pods) == self.members and all(
                    any(
                        c["type"] == "Ready" and c["status"] == "True"
                        for c in p.get("status", {}).get("conditions", [])
                    )
                    for p in pods
                ):
                    return
            time.sleep(3)
        raise RuntimeError("PostgreSQL cluster did not converge to its required primary and replica count")

    def run_client(self, mode, **arguments):
        source = Path(__file__).with_name("gitea_workflow.py").read_text()
        # The script and arguments travel on stdin; no host ports or local auth files are needed.
        source += "\nrun(" + repr(mode) + ", **json.loads(" + repr(json.dumps(arguments)) + "))\n"
        output = self.command("exec", "-i", "application-client", "--", "python", "-", input_text=source, timeout=180)
        return json.loads(output)

    def check_workflow(self, token=None):
        result = self.run_client("probe", token=token or secrets.token_hex(16))
        stored = self.command(
            "exec",
            "deployment/gitea",
            "--",
            "su-exec",
            "git",
            "git",
            "--git-dir",
            "/data/git/repositories/zoo-labs/zoo-utilities.git",
            "show",
            f"HEAD:sregym/{result['token']}.txt",
        )
        if stored != result["token"]:
            raise RuntimeError("Acknowledged file is missing from the Git repository")
        return result

    def create_workload(self, rate=10, **kwargs):
        self.wrk = Wrk2WorkloadManager(
            wrk=Wrk2(rate=rate, namespace=self.namespace, **kwargs),
            payload_script=Path(__file__).with_name("gitea_workload.lua"),
            url="{placeholder}",
            namespace=self.namespace,
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.url = get_frontend_url(self)
        self.wrk.start()

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()

    def cleanup(self):
        self.stop_workload()
        cleanup_persistent_namespace(self)

    def delete(self):
        self.cleanup()
