"""Infrahub (OpsMill) infrastructure data platform, deployed like its production docker-compose.

Topology: ``infrahub-server`` (gunicorn, 4 API workers), two Prefect ``task-worker``
pods (``infrahubasync`` workers, pool ``infrahub-worker``), the Prefect 3
``task-manager`` on PostgreSQL, Neo4j, RabbitMQ and Redis without persistence.
All images are upstream. The inventory-sync workload is a standard-library
Python script shipped in a ConfigMap and run in a stock ``python`` image; the
conductor's probes are sent to the application-client pod on stdin and never
stored in the cluster.
"""

import json
import secrets
import subprocess
import time
import uuid
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.client_runner import PythonClientRunner
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.kubectl import KubeCtl

INFRAHUB_VERSION = "1.11.9"
INFRAHUB_IMAGE = f"registry.opsmill.io/opsmill/infrahub:{INFRAHUB_VERSION}"
NEO4J_IMAGE = "neo4j:2026.05.0-community"
RABBITMQ_IMAGE = "rabbitmq:4.2.1-management"
REDIS_IMAGE = "redis:8.4.0"
POSTGRES_IMAGE = "postgres:18-alpine"
PYTHON_IMAGE = "python:3.12.13-alpine3.23"

RUNTIME = Path(__file__).with_name("runtime")
BASE_FIELDS = {"NetworkDevice": ["role", "site"], "NetworkModule": ["device", "slot"]}
CHANGE_BRANCHES = " It also prepares its changes on branches and merges each into main after review."
RELEASE_PLAN = " It also keeps the branches listed in its release plan merged into main."


class InfrahubApplication(KubernetesCommands, Application):
    app_id = "infrahub"
    metadata_file = Path(__file__).with_name("metadata.json")
    frontend_port = 8000
    startup_timeout = 1200
    #: Deployments whose replica count is part of the healthy topology.
    topology = {
        "database": 1,
        "message-queue": 1,
        "cache": 1,
        "task-manager-db": 1,
        "task-manager": 1,
        "infrahub-server": 1,
        "task-worker": 2,
        "inventory-sync": 1,
    }
    volumes = ("database-data", "task-manager-db-data", "infrahub-storage")

    def __init__(self):
        super().__init__(self.metadata_file)
        self.load_app_json()
        self.app_name = self.name
        self.base_description = self.get_app_json()["Desc"]
        self.description = self.base_description
        self.kubectl = KubeCtl()
        self.frontend_service = "infrahub-server"
        self.helm_deploy = False
        self.mount_failure_scripts = False
        #: Environment of the inventory-sync workload; problems adjust it before deploy.
        self.workload_env = {"CHANGE_INTERVAL": "0"}
        #: Attributes inventory-sync sets on every object it records, per kind (its config file).
        self.inventory_fields = {kind: list(fields) for kind, fields in BASE_FIELDS.items()}
        #: Values inventory-sync rotates through per attribute (for Dropdown attributes); empty by default.
        self.inventory_values: dict[str, list[str]] = {}
        #: Schema options of the bootstrap: extra NetworkDevice attributes, Dropdown attributes and
        #: their choices, and the values the seeded devices get for them.
        self.device_extra: list[str] = []
        self.device_dropdowns: dict[str, list[str]] = {}
        self.seed_values: dict[str, list[str]] = {}
        self.seed_devices = 24
        #: History created by the bootstrap: older plan branches, teams' branches with their own schema
        #: changes ({branch: {"attrs": [...], "prefix": ..., "devices": n}}), and a later release on main.
        self.history_branches = 0
        self.team_branches: dict[str, dict] = {}
        self.main_release: list[str] = []
        #: Branches inventory-sync's release plan keeps merged into main.
        self.inventory_releases: list[str] = []
        self.server_memory_limit = "4Gi"
        self.seeded_devices: list[str] = []
        self.seeded_modules: list[str] = []
        self.describe()

    # ------------------------------------------------------------------ manifests
    @staticmethod
    def secret_env(name, key):
        return {"name": name, "valueFrom": {"secretKeyRef": {"name": "infrahub-credentials", "key": key}}}

    def pvc(self, name, size):
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}},
        }

    @staticmethod
    def service(name, port):
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {"selector": {"app": name}, "ports": [{"name": "tcp", "port": port, "targetPort": port}]},
        }

    def deployment(self, name, container, replicas=1, volumes=(), recreate=True, **pod):
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "labels": {"app": name, "app.kubernetes.io/part-of": "infrahub"}},
            "spec": {
                "replicas": replicas,
                "progressDeadlineSeconds": self.startup_timeout,
                "strategy": {"type": "Recreate"} if recreate else {"type": "RollingUpdate"},
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name, "app.kubernetes.io/part-of": "infrahub"}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "containers": [{"name": name, "imagePullPolicy": "IfNotPresent", **container}],
                        "volumes": list(volumes),
                        **pod,
                    },
                },
            },
        }

    def infrahub_env(self, log_level):
        values = {
            "INFRAHUB_PRODUCTION": "true",
            "INFRAHUB_LOG_LEVEL": log_level,
            "INFRAHUB_ALLOW_ANONYMOUS_ACCESS": "false",
            "INFRAHUB_TELEMETRY_OPTOUT": "true",
            "INFRAHUB_ANALYTICS_ENABLE": "false",
            "INFRAHUB_DB_ADDRESS": "database",
            "INFRAHUB_DB_USERNAME": "neo4j",
            "INFRAHUB_BROKER_ADDRESS": "message-queue",
            "INFRAHUB_BROKER_USERNAME": "infrahub",
            "INFRAHUB_CACHE_ADDRESS": "cache",
            "INFRAHUB_WORKFLOW_ADDRESS": "task-manager",
            "INFRAHUB_WORKFLOW_PORT": "4200",
            "PREFECT_API_URL": "http://task-manager:4200/api",
            "INFRAHUB_ADDRESS": "http://infrahub-server:8000",
            "INFRAHUB_INTERNAL_ADDRESS": "http://infrahub-server:8000",
            "INFRAHUB_PUBLIC_URL": "http://infrahub-server:8000",
            "INFRAHUB_STORAGE_DRIVER": "local",
            "INFRAHUB_STORAGE_LOCAL_PATH": "/opt/infrahub/storage",
            "INFRAHUB_GIT_REPOSITORIES_DIRECTORY": "/opt/infrahub/git",
            "INFRAHUB_GIT_GLOBAL_CONFIG_FILE": "/tmp/infrahub-gitconfig",
            "HOME": "/tmp",
        }
        return [{"name": k, "value": v} for k, v in values.items()] + [
            self.secret_env("INFRAHUB_DB_PASSWORD", "db-password"),
            self.secret_env("INFRAHUB_BROKER_PASSWORD", "broker-password"),
            self.secret_env("INFRAHUB_SECURITY_SECRET_KEY", "secret-key"),
            self.secret_env("INFRAHUB_INITIAL_ADMIN_PASSWORD", "admin-password"),
            self.secret_env("INFRAHUB_INITIAL_ADMIN_TOKEN", "admin-token"),
            self.secret_env("INFRAHUB_INITIAL_AGENT_TOKEN", "agent-token"),
        ]

    def backing_documents(self):
        storage = [{"name": "data", "persistentVolumeClaim": {"claimName": "database-data"}}]
        return [
            self.pvc("database-data", "5Gi"),
            self.pvc("task-manager-db-data", "2Gi"),
            self.pvc("infrahub-storage", "1Gi"),
            self.deployment(
                "database",
                {
                    "image": NEO4J_IMAGE,
                    "env": [
                        {
                            "name": "NEO4J_AUTH",
                            "valueFrom": {"secretKeyRef": {"name": "infrahub-credentials", "key": "neo4j-auth"}},
                        },
                        {"name": "NEO4J_dbms_security_procedures_unrestricted", "value": "apoc.*"},
                        {"name": "NEO4J_dbms_security_auth__minimum__password__length", "value": "4"},
                        {"name": "NEO4J_server_memory_heap_initial__size", "value": "512m"},
                        {"name": "NEO4J_server_memory_heap_max__size", "value": "1500m"},
                        {"name": "NEO4J_server_memory_pagecache_size", "value": "512m"},
                    ],
                    "ports": [{"containerPort": 7687}],
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    "readinessProbe": {"tcpSocket": {"port": 7687}, "periodSeconds": 5},
                    "resources": {"requests": {"cpu": "500m", "memory": "2Gi"}, "limits": {"memory": "3Gi"}},
                },
                volumes=storage,
            ),
            self.deployment(
                "message-queue",
                {
                    "image": RABBITMQ_IMAGE,
                    "env": [
                        {"name": "RABBITMQ_DEFAULT_USER", "value": "infrahub"},
                        self.secret_env("RABBITMQ_DEFAULT_PASS", "broker-password"),
                    ],
                    "ports": [{"containerPort": 5672}],
                    "readinessProbe": {"tcpSocket": {"port": 5672}, "periodSeconds": 5},
                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
                },
                hostname="message-queue",
            ),
            self.deployment(
                "cache",
                {
                    "image": REDIS_IMAGE,
                    # Cache only: no RDB snapshots, no AOF (a restart starts empty).
                    "args": ["redis-server", "--save", "", "--appendonly", "no"],
                    "ports": [{"containerPort": 6379}],
                    "readinessProbe": {"exec": {"command": ["redis-cli", "ping"]}, "periodSeconds": 2},
                    "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "512Mi"}},
                },
            ),
            self.deployment(
                "task-manager-db",
                {
                    "image": POSTGRES_IMAGE,
                    "env": [
                        {"name": "POSTGRES_USER", "value": "postgres"},
                        self.secret_env("POSTGRES_PASSWORD", "pg-password"),
                        {"name": "POSTGRES_DB", "value": "prefect"},
                        {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                    ],
                    "ports": [{"containerPort": 5432}],
                    "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}],
                    "readinessProbe": {
                        "exec": {"command": ["pg_isready", "-q", "-d", "prefect", "-U", "postgres"]},
                        "periodSeconds": 5,
                    },
                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
                },
                volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "task-manager-db-data"}}],
            ),
            self.service("database", 7687),
            self.service("message-queue", 5672),
            self.service("cache", 6379),
            self.service("task-manager-db", 5432),
        ]

    def task_manager_documents(self):
        return [
            self.deployment(
                "task-manager",
                {
                    "image": INFRAHUB_IMAGE,
                    "command": [
                        "uvicorn",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "4200",
                        "--factory",
                        "infrahub.prefect_server.app:create_infrahub_prefect",
                    ],
                    "env": [
                        self.secret_env("PREFECT_API_DATABASE_CONNECTION_URL", "prefect-database-url"),
                        {"name": "PREFECT_SERVER_ANALYTICS_ENABLED", "value": "false"},
                        {"name": "HOME", "value": "/tmp"},
                    ],
                    "ports": [{"containerPort": 4200}],
                    "readinessProbe": {"httpGet": {"path": "/api/health", "port": 4200}, "periodSeconds": 5},
                    "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}},
                },
                recreate=False,
            ),
            self.service("task-manager", 4200),
        ]

    def server_documents(self):
        return [
            self.deployment(
                "infrahub-server",
                {
                    "image": INFRAHUB_IMAGE,
                    "command": [
                        "gunicorn",
                        "--config",
                        "backend/infrahub/serve/gunicorn_config.py",
                        "-w",
                        "4",
                        "--timeout",
                        "300",
                        "--logger-class",
                        "infrahub.serve.log.GunicornLogger",
                        "infrahub.server:app",
                    ],
                    "env": self.infrahub_env("INFO"),
                    "ports": [{"containerPort": 8000}],
                    "volumeMounts": [{"name": "storage", "mountPath": "/opt/infrahub/storage"}],
                    "readinessProbe": {
                        "httpGet": {"path": "/api/config", "port": 8000},
                        "periodSeconds": 5,
                        "timeoutSeconds": 5,
                    },
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "1536Mi"},
                        "limits": {"memory": self.server_memory_limit},
                    },
                },
                volumes=[{"name": "storage", "persistentVolumeClaim": {"claimName": "infrahub-storage"}}],
            ),
            self.service("infrahub-server", 8000),
        ]

    def worker_documents(self):
        env = self.infrahub_env("INFO") + [
            self.secret_env("INFRAHUB_API_TOKEN", "agent-token"),
            {"name": "INFRAHUB_TIMEOUT", "value": "60"},
        ]
        return [
            self.deployment(
                "task-worker",
                {
                    "image": INFRAHUB_IMAGE,
                    "command": [
                        "prefect",
                        "worker",
                        "start",
                        "--type",
                        "infrahubasync",
                        "--pool",
                        "infrahub-worker",
                        "--with-healthcheck",
                    ],
                    "env": env,
                    "readinessProbe": {
                        "exec": {"command": ["/bin/sh", "-c", "curl -fsS localhost:8080/health >/dev/null"]},
                        "periodSeconds": 10,
                        "timeoutSeconds": 5,
                    },
                    "resources": {"requests": {"cpu": "250m", "memory": "768Mi"}, "limits": {"memory": "3Gi"}},
                },
                replicas=2,
                recreate=False,
            )
        ]

    def inventory_configmaps(self):
        return [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "inventory-sync"},
                "data": {"inventory_sync.py": (RUNTIME / "inventory_sync.py").read_text()},
            },
            self.inventory_config(),
        ]

    def inventory_config(self):
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "inventory-sync-config"},
            "data": {"fields.json": json.dumps(self.inventory_settings(), indent=2) + "\n"},
        }

    def inventory_settings(self):
        settings = {kind: list(fields) for kind, fields in self.inventory_fields.items()}
        if self.inventory_values:
            settings["values"] = {field: list(values) for field, values in self.inventory_values.items()}
        if self.inventory_releases:
            settings["releases"] = list(self.inventory_releases)
        return settings

    def python_container(self, name, command, env=()):
        return {
            "name": name,
            "image": PYTHON_IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "command": command,
            "env": [
                {"name": "PYTHONUNBUFFERED", "value": "1"},
                self.secret_env("INFRAHUB_API_TOKEN", "admin-token"),
                *env,
            ],
            "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"memory": "256Mi"}},
        }

    def client_document(self):
        container = self.python_container("client", ["sleep", "infinity"])
        container["volumeMounts"] = [{"name": "credentials", "mountPath": "/credentials", "readOnly": True}]
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "application-client"},
            "spec": {
                "automountServiceAccountToken": False,
                "containers": [container],
                "volumes": [{"name": "credentials", "secret": {"secretName": "infrahub-credentials"}}],
            },
        }

    def workload_document(self):
        env = [{"name": k, "value": str(v)} for k, v in sorted(self.workload_env.items())]
        container = self.python_container(
            "inventory-sync", ["python", "-u", "/opt/inventory-sync/inventory_sync.py"], env
        )
        container.pop("name")
        container.pop("imagePullPolicy")
        container["volumeMounts"] = [
            {"name": "app", "mountPath": "/opt/inventory-sync", "readOnly": True},
            {"name": "config", "mountPath": "/etc/inventory-sync", "readOnly": True},
        ]
        return self.deployment(
            "inventory-sync",
            container,
            volumes=[
                {"name": "app", "configMap": {"name": "inventory-sync"}},
                {"name": "config", "configMap": {"name": "inventory-sync-config"}},
            ],
            recreate=False,
        )

    def describe(self):
        """The agent-facing description: the components, plus what the workload does in this deployment."""
        changes = int(self.workload_env.get("CHANGE_INTERVAL", 0)) > 0
        text = (CHANGE_BRANCHES if changes else "") + (RELEASE_PLAN if self.inventory_releases else "")
        self.description = self.base_description.replace(" {changes}", text)
        return self.description

    # ------------------------------------------------------------------ lifecycle
    def ensure_secret(self):
        if self.command("get", "secret", "infrahub-credentials", "--ignore-not-found", "-o", "name").strip():
            return
        pg = secrets.token_hex(16)
        db = secrets.token_hex(16)
        data = {
            "db-password": db,
            "neo4j-auth": f"neo4j/{db}",
            "broker-password": secrets.token_hex(16),
            "pg-password": pg,
            "prefect-database-url": f"postgresql+asyncpg://postgres:{pg}@task-manager-db:5432/prefect",
            "secret-key": str(uuid.uuid4()),
            "admin-password": secrets.token_hex(12),
            "admin-token": str(uuid.uuid4()),
            "agent-token": str(uuid.uuid4()),
        }
        self.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "infrahub-credentials"},
                    "type": "Opaque",
                    "stringData": data,
                }
            ]
        )

    def rollout(self, *names, timeout=None):
        timeout = timeout or self.startup_timeout
        for name in names:
            self.command("rollout", "status", f"deployment/{name}", f"--timeout={timeout}s", timeout=timeout + 30)

    def deploy(self, attempts=3):
        """Deploy; start over from an empty namespace when Infrahub's first initialization does not complete.

        A first boot that dies half-way (for example a worker killed on a slow, loaded host) leaves
        the database half-initialized, and the server then never starts.
        """
        for attempt in range(1, attempts + 1):
            try:
                return self._deploy()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError):
                if attempt == attempts:
                    raise
                self.logger.warning(f"Infrahub deployment attempt {attempt} did not come up; redeploying from scratch")
                self._delete_namespace()

    def _delete_namespace(self):
        self.cleanup()
        deadline = time.monotonic() + 900
        while self.command("get", "namespace", self.namespace, "--ignore-not-found", "-o", "name").strip():
            if time.monotonic() > deadline:
                raise RuntimeError(f"namespace {self.namespace} was not deleted")
            time.sleep(5)

    def _deploy(self):
        self.create_namespace()
        self.ensure_secret()
        self.apply(self.backing_documents())
        self.rollout("database", "message-queue", "cache", "task-manager-db")
        self.apply(self.task_manager_documents())
        self.rollout("task-manager")
        self.apply(self.server_documents())
        self.wait_first_boot()
        self.apply(self.worker_documents())
        self.rollout("task-worker")
        self.apply([self.client_document()])
        self.command("wait", "--for=condition=Ready", "pod/application-client", "--timeout=600s", timeout=620)
        seeded = self.run_client(
            "bootstrap",
            exec_timeout=1800,
            devices=self.seed_devices,
            device_extra=self.device_extra,
            dropdowns=self.device_dropdowns,
            seed_values=self.seed_values,
            history_branches=self.history_branches,
            team_branches=self.team_branches,
            main_release=self.main_release,
        )
        self.seeded_devices, self.seeded_modules = seeded["seeded"], seeded["modules"]
        self.wait_workers_registered()

    def wait_first_boot(self):
        """Wait for the server's first start; a crash during it is fatal (half-initialized database)."""
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            pods = self.pods("infrahub-server")
            statuses = [c for p in pods for c in p.get("status", {}).get("containerStatuses", [])]
            if any(c.get("restartCount", 0) for c in statuses):
                raise RuntimeError("infrahub-server crashed during its first initialization")
            if statuses and all(c.get("ready") for c in statuses):
                return
            time.sleep(10)
        raise RuntimeError("infrahub-server did not become ready")

    def wait_workers_registered(self, timeout=300):
        """Both task workers polling the work pool and four API workers heartbeating."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            api = len(self.redis("--scan", "--pattern", "workers:active:api_server:*").split())
            workers = len(self.redis("--scan", "--pattern", "workers:active:git_agent:*").split())
            if api >= 4 and workers >= 2:
                return
            time.sleep(3)
        raise RuntimeError("Infrahub API or task workers did not register in Redis")

    def create_workload(self, **kwargs):
        self.workload_env.update({k: str(v) for k, v in kwargs.items()})
        self.describe()

    def start_workload(self):
        self.apply([*self.inventory_configmaps(), self.workload_document()])
        self.rollout("inventory-sync", timeout=600)

    def set_inventory_fields(self, kind, fields):
        """Change inventory-sync's configured attributes for ``kind`` (it rereads its config file)."""
        self.inventory_fields[kind] = list(fields)
        self.apply([self.inventory_config()])

    def set_inventory_releases(self, branches):
        """Change the branches inventory-sync's release plan keeps merged (it rereads its config file)."""
        self.inventory_releases = list(branches)
        self.apply([self.inventory_config()])

    def set_inventory_values(self, field, values):
        """Change the values inventory-sync rotates through for ``field`` (it rereads its config file)."""
        self.inventory_values[field] = list(values)
        self.apply([self.inventory_config()])

    def configured_inventory_fields(self):
        found = self.command("get", "configmap", "inventory-sync-config", "--ignore-not-found", "-o", "json").strip()
        if not found:
            return None
        try:
            return json.loads(json.loads(found)["data"]["fields.json"])
        except (KeyError, ValueError):
            return None

    def redeploy(self):
        """Replace the whole namespace with a fresh deployment (a clean slate for a retried injection)."""
        self._delete_namespace()
        self.deploy()
        self.start_workload()

    def stop_workload(self):
        self.command("delete", "deployment", "inventory-sync", "--ignore-not-found", "--wait=true")

    def cleanup(self):
        self.kubectl.delete_namespace(self.namespace)

    def delete(self):
        self.cleanup()

    # ------------------------------------------------------------------ helpers
    def run_client(self, mode, exec_timeout=None, **arguments):
        """Run a probe workflow in the client pod; ``exec_timeout`` bounds the whole call."""
        if exec_timeout is None:
            exec_timeout = int(arguments.get("timeout", 180)) + 180
        return PythonClientRunner(self.command).run(
            [RUNTIME / "infrahub_client.py", RUNTIME / "workflow.py"],
            f"print(json.dumps(workflow(Infrahub(), {mode!r}, **json.loads({json.dumps(arguments)!r}))))",
            timeout=exec_timeout,
        )

    def redis(self, *args, timeout=60, attempts=3):
        """Run redis-cli in the cache pod (retried: the pod may be restarting or the API slow)."""
        for attempt in range(attempts):
            try:
                return self.command("exec", "deploy/cache", "--", "redis-cli", "--raw", *args, timeout=timeout)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt == attempts - 1:
                    raise
                time.sleep(5)

    def cypher(self, query, timeout=120):
        """Run a read query with cypher-shell in the database pod; return the plain-format rows."""
        script = 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}" --format plain "$0"'
        out = self.command("exec", "deploy/database", "--", "sh", "-c", script, query, timeout=timeout)
        return [line for line in out.splitlines()[1:] if line.strip()]

    def pods(self, app):
        return json.loads(self.command("get", "pods", "-l", f"app={app}", "-o", "json"))["items"]

    def volume_ids(self):
        claims = json.loads(self.command("get", "pvc", "-o", "json"))["items"]
        return {p["metadata"]["name"]: p["metadata"]["uid"] for p in claims}

    def logs(self, pod, since=None, tail=None):
        args = ["logs", pod]
        if since:
            args.append(f"--since-time={since}")
        if tail:
            args.append(f"--tail={tail}")
        return self.command(*args, timeout=60)
