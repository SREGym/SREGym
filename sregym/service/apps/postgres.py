"""Shared PostgreSQL deployment and lifecycle helpers for application adapters."""

import json
import secrets
import time
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.client_runner import PythonClientRunner
from sregym.service.apps.components.cnpg import PostgresCluster, ensure_operator
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.apps.persistent import cleanup_persistent_namespace
from sregym.service.kubectl import KubeCtl

POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:16.14-system-trixie"
TIERS = {"single": 1, "replicated": 3}


class PostgresApplication(KubernetesCommands, Application):
    slug = ""
    frontend_port = 80
    health_path = "/"
    startup_timeout = 900
    data_volumes = ()
    auxiliary_deployments = ()
    client_workflow: Path
    metadata_file: Path

    def __init__(self, tier="replicated", storage_class="openebs-hostpath"):
        if tier not in TIERS:
            raise ValueError(f"Unsupported tier {tier!r}: choose {tuple(TIERS)}")
        super().__init__(self.metadata_file)
        self.scale_tier, self.members, self.storage_class = tier, TIERS[tier], storage_class
        self.load_app_json()
        self.app_name = self.name
        self.description = self.get_app_json()["Desc"]
        self.kubectl = KubeCtl()
        self.frontend_service = self.slug
        self.database = self.slug.replace("-", "_")
        self.cluster_name = f"{self.slug}-db"
        self.helm_deploy = False
        self.mount_failure_scripts = False

    def get_app_json(self):
        result = super().get_app_json()
        if hasattr(self, "members"):
            result["Desc"] += f" Tier {self.scale_tier}: {self.members} PostgreSQL members."
        return result

    @property
    def postgres(self) -> PostgresCluster:
        return PostgresCluster(self.command, self.cluster_name, self.database)

    def pvc(self, name, size="2Gi"):
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": self.storage_class,
                "resources": {"requests": {"storage": size}},
            },
        }

    @property
    def expected_volume_count(self):
        return self.members + len(self.data_volumes)

    def secret_env(self, name, key, secret="application-credentials"):
        return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}

    def database_document(self):
        postgres = {"parameters": {"shared_buffers": "128MB", "max_connections": "200", "max_wal_size": "256MB"}}
        if self.members > 1:
            postgres["synchronous"] = {"method": "any", "number": 1, "dataDurability": "required"}
        return self.postgres.document(
            {
                "instances": self.members,
                "imageName": POSTGRES_IMAGE,
                # Bound graceful shutdown for these small disposable datasets.
                # The default 30-minute pod grace also delays namespace rescans.
                "smartShutdownTimeout": 30,
                "stopDelay": 90,
                "bootstrap": {
                    "initdb": {
                        "database": self.database,
                        "owner": self.database,
                        "secret": {"name": "application-database"},
                        "postInitApplicationSQL": [
                            "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
                            "CREATE EXTENSION IF NOT EXISTS btree_gist;",
                        ],
                    }
                },
                "storage": {"size": "2Gi", "storageClass": self.storage_class},
                "postgresql": postgres,
                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
                "affinity": {"enablePodAntiAffinity": True, "podAntiAffinityType": "preferred"},
            }
        )

    def service(self, name, port):
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {"selector": {"app": name}, "ports": [{"port": port, "targetPort": port}]},
        }

    def deployment(self, name, container, volumes=(), **pod_options):
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {
                "replicas": 1,
                "progressDeadlineSeconds": self.startup_timeout + 60,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [container],
                        "volumes": list(volumes),
                        **pod_options,
                    },
                },
            },
        }

    def probes(self, path=None):
        check = {
            "httpGet": {"path": path or self.health_path, "port": self.frontend_port},
            "timeoutSeconds": 5,
            "periodSeconds": 5,
        }
        return {"readinessProbe": check, "startupProbe": {**check, "failureThreshold": self.startup_timeout // 5}}

    def render(self):
        client = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "application-client"},
            "spec": {
                "automountServiceAccountToken": False,
                "volumes": [{"name": "credentials", "secret": {"secretName": "application-credentials"}}],
                "containers": [
                    {
                        "name": "client",
                        "image": "python:3.12.13-alpine3.23",
                        "command": ["sleep", "infinity"],
                        "volumeMounts": [{"name": "credentials", "mountPath": "/credentials", "readOnly": True}],
                        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "256Mi"}},
                    }
                ],
            },
        }
        return [
            self.database_document(),
            self.service(self.slug, self.frontend_port),
            client,
            *[self.pvc(name) for name in self.data_volumes],
            *self.application_documents(),
        ]

    def deploy(self):
        self.command("get", "storageclass", self.storage_class)
        ensure_operator(self.command)
        self.create_namespace()
        for name, data in (
            ("application-database", {"username": self.database, "password": secrets.token_hex(24)}),
            (
                "application-credentials",
                {
                    "username": "application",
                    "password": secrets.token_hex(24),
                    "token": "sk_test_" + secrets.token_hex(24),
                },
            ),
        ):
            if not self.command("get", "secret", name, "--ignore-not-found", "-o", "name").strip():
                self.apply(
                    [
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {"name": name},
                            "type": "kubernetes.io/basic-auth",
                            "stringData": data,
                        }
                    ]
                )
        documents = self.render()
        self.apply([d for d in documents if d["kind"] == "Cluster"])
        self.wait_database()
        self.apply([d for d in documents if d["kind"] != "Cluster"])
        for name in (*self.auxiliary_deployments, self.slug):
            self.command(
                "rollout",
                "status",
                f"deployment/{name}",
                f"--timeout={self.startup_timeout}s",
                timeout=self.startup_timeout + 30,
            )
        self.command("wait", "--for=condition=Ready", "pod/application-client", "--timeout=300s", timeout=320)
        if not self.command("get", "configmap", "business-data-seeded", "--ignore-not-found", "-o", "name").strip():
            self.initialize()
            self.run_client("seed")
            self.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "business-data-seeded"},
                        "data": {"version": "1"},
                    }
                ]
            )
        self.check_workflow()

    def initialize(self):
        pass

    def cluster(self):
        return self.postgres.get()

    def database_pods(self):
        return self.postgres.pods()

    def sql(self, statement, pod=None):
        return self.postgres.sql(statement, pod or self.cluster()["status"]["currentPrimary"])

    def wait_database(self, timeout=600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.cluster().get("status", {})
            pods = self.database_pods()
            if (
                status.get("readyInstances") == self.members
                and status.get("currentPrimary")
                and status["currentPrimary"] == status.get("targetPrimary")
                and len(pods) == self.members
                and all(
                    any(
                        c["type"] == "Ready" and c["status"] == "True"
                        for c in p.get("status", {}).get("conditions", [])
                    )
                    for p in pods
                )
            ):
                return
            time.sleep(3)
        raise RuntimeError("PostgreSQL primary and replicas did not converge")

    def run_client(self, mode, **arguments):
        url = f"http://{self.slug}:{self.frontend_port}"
        return PythonClientRunner(self.command).run(
            [(Path(__file__).with_name("components") / "http_client.py"), self.client_workflow],
            f"print(json.dumps(workflow(Client({url!r}), {mode!r}, **json.loads({json.dumps(arguments)!r}))))",
        )

    def check_workflow(self, token=None):
        return self.run_client("probe", token=token or secrets.token_hex(16))

    def create_workload(self, rate=2, **kwargs):
        self.workload_rate = rate

    def start_workload(self):
        url = f"http://{self.slug}:{self.frontend_port}{self.health_path}"
        interval = 1 / getattr(self, "workload_rate", 2)
        source = (
            "import time,urllib.request\nwhile True:\n try:\n"
            f"  urllib.request.urlopen({url!r},timeout=3).read()\n except Exception: pass\n time.sleep({interval!r})"
        )
        self.apply(
            [
                self.deployment(
                    "application-traffic",
                    {
                        "name": "traffic",
                        "image": "python:3.12.13-alpine3.23",
                        "command": ["python", "-u", "-c", source],
                        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}},
                    },
                )
            ]
        )

    def stop_workload(self):
        self.command("delete", "deployment", "application-traffic", "--ignore-not-found", "--wait=true")

    def cleanup(self):
        cleanup_persistent_namespace(self)

    def delete(self):
        self.cleanup()
