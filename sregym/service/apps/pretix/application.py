"""pretix ticket shop, deployed like pretix's production docker setup (deployment/docker assets).

Topology: ``pretix-web`` (supervisord: nginx in front of gunicorn, 12 workers x 16
threads), ``pretix-worker`` (Celery, concurrency 4), the ``pretix-periodic`` CronJob
(``pretix cron`` every minute, concurrencyPolicy Forbid), PostgreSQL, Redis (AOF on a
PVC) and a Mailpit SMTP sink. All images are upstream. The organizer runs a one-day
workshop event with limited seats per workshop and an automatic waiting list; the
door-scanner Deployment (stock python image, one script in a ConfigMap) checks
attendees in through the check-in API like a door scanning station.

Buyer traffic is not part of the cluster: problems drive it from the conductor host
through the NodePort front door (``shop.ShopTraffic``), like customers on the internet.
"""

import json
import secrets
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.kubectl import KubeCtl

PRETIX_VERSION = "2026.9.0"
PRETIX_IMAGE = (
    f"docker.io/pretix/standalone:{PRETIX_VERSION}"
    "@sha256:df8bf48989affa698e88304c62f11b902e0d0655f6fb22eeb11ba7cc0185fa7d"
)
POSTGRES_IMAGE = "docker.io/library/postgres@sha256:3518071376d0b6156b02e1db2747a78511b5b9dc3763cc79c506ebe2dc2ee71c"
REDIS_IMAGE = "docker.io/library/redis@sha256:026c880268ce52fb3a63b66150af583108a38c13af7633e325ae82a145f53a87"
MAILPIT_IMAGE = "docker.io/axllent/mailpit@sha256:c17238042fcdd49d399934c56a42a896a32efdb857210556ea8b40cef4934f4c"
PYTHON_IMAGE = "python:3.12.13-alpine3.23"

HERE = Path(__file__).parent
CONFIG = HERE / "config"
RUNTIME = HERE / "runtime"

ORGANIZER = "lakeside"
EVENT = "techday26"
#: Workshops sold in the shop: (title, room, seats). Each is a product with its own quota.
WORKSHOPS = (
    ("Kubernetes operators from scratch", "Room A1", 16),
    ("Rust for Python developers", "Room A2", 14),
    ("Practical PostgreSQL performance", "Room B1", 16),
    ("Observability with OpenTelemetry", "Room B2", 12),
    ("Accessible web components", "Room C1", 14),
    ("Terraform module design", "Room C2", 12),
    ("Event sourcing in practice", "Room D1", 16),
    ("Securing CI/CD pipelines", "Room D2", 14),
    ("Data pipelines with DuckDB", "Room E1", 12),
    ("Incident response game day", "Room E2", 16),
    ("GraphQL schema evolution", "Room F1", 14),
    ("Edge computing with WebAssembly", "Room F2", 12),
)
UID = 15371
#: Port of the door stations' handheld-reader endpoint (only when a profile enables it).
HANDHELD_PORT = 8000

#: Deployment profile. The defaults are the original workshop shop; a problem may switch on
#: ordinary organizer settings (ticket downloads, close-when-sold-out quotas, a shorter cart
#: reservation) and door-station features, and may override the agent-facing description.
DEFAULT_PROFILE = {
    "description": None,
    "ticket_download": False,
    "close_when_sold_out": False,
    "reservation_minutes": None,
    "scanner_walk_in": True,
    "scanner_handheld": False,
    "scanner_waitlist_watch": False,
}


def _security(uid):
    return {"runAsNonRoot": True, "runAsUser": uid, "fsGroup": uid, "seccompProfile": {"type": "RuntimeDefault"}}


class PretixApplication(KubernetesCommands, Application):
    app_id = "pretix"
    metadata_file = HERE / "metadata.json"
    frontend_port = 80
    startup_timeout = 900
    topology = {"postgres": 1, "redis": 1, "mailpit": 1, "pretix-web": 1, "pretix-worker": 1, "door-scanner": 1}
    volumes = ("postgres-data", "redis-data", "mailpit-data", "pretix-data")

    def __init__(self, profile=None):
        super().__init__(self.metadata_file)
        self.load_app_json()
        self.app_name = self.name
        self.profile = {**DEFAULT_PROFILE, **(profile or {})}
        self.description = self.profile["description"] or self.get_app_json()["Desc"]
        self.kubectl = KubeCtl()
        self.frontend_service = "pretix-web"
        self.helm_deploy = False
        self.mount_failure_scripts = False
        #: Filled by bootstrap: organizer/event ids, products, quotas, check-in lists, tokens.
        self.catalog: dict = {}

    # ------------------------------------------------------------------ manifests
    @staticmethod
    def _meta(name, **extra):
        return {"name": name, "labels": {"app": name, "app.kubernetes.io/part-of": "pretix"}, **extra}

    def pvc(self, name, size):
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}},
        }

    @staticmethod
    def service(name, port, target=None, node_port=False):
        spec = {
            "selector": {"app": name},
            "ports": [{"name": "http" if node_port else name, "port": port, "targetPort": target or port}],
        }
        if node_port:
            spec["type"] = "NodePort"
        return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name}, "spec": spec}

    @staticmethod
    def container(name, image, **kw):
        return {
            "name": name,
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            **kw,
        }

    def pod_template(self, name, uid, containers, volumes=(), annotations=None):
        meta = {"labels": {"app": name, "app.kubernetes.io/part-of": "pretix"}}
        if annotations:
            meta["annotations"] = annotations
        spec = {
            "enableServiceLinks": False,
            "automountServiceAccountToken": False,
            "securityContext": _security(uid),
            "containers": containers,
        }
        if volumes:
            spec["volumes"] = list(volumes)
        return {"metadata": meta, "spec": spec}

    def deployment(self, name, template):
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": self._meta(name),
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": name}},
                "template": template,
            },
        }

    def config_files(self):
        return {name: (CONFIG / name).read_text() for name in ("pretix.cfg", "nginx.conf", "supervisor.conf")}

    def app_volumes(self):
        return [
            {"name": "pretix-data", "persistentVolumeClaim": {"claimName": "pretix-data"}},
            {"name": "pretix-config", "configMap": {"name": "pretix-config"}},
            {"name": "system-password", "secret": {"secretName": "pretix-system"}},
        ]

    @staticmethod
    def app_mounts():
        return [
            {"name": "pretix-data", "mountPath": "/data"},
            {"name": "pretix-config", "mountPath": "/etc/pretix"},
            {"name": "system-password", "mountPath": "/run/pretix-credentials", "readOnly": True},
        ]

    @staticmethod
    def app_env():
        # The image's entrypoint (deployment/docker/pretix.bash) exports these for every pretix process;
        # pretix-web runs supervisord directly, so they are set here for all of them alike.
        return [
            {"name": "AUTOMIGRATE", "value": "skip"},
            {"name": "FILE__PRETIX_DATABASE_PASSWORD", "value": "/run/pretix-credentials/db-password"},
            {"name": "DJANGO_SETTINGS_MODULE", "value": "production_settings"},
            {"name": "DATA_DIR", "value": "/data/"},
            {"name": "HOME", "value": "/pretix"},
        ]

    def stores_documents(self):
        secret_vol = {"name": "system-password", "secret": {"secretName": "pretix-system"}}
        return [
            self.pvc("postgres-data", "8Gi"),
            self.pvc("redis-data", "2Gi"),
            self.pvc("mailpit-data", "1Gi"),
            self.pvc("pretix-data", "8Gi"),
            self.service("postgres", 5432),
            self.service("redis", 6379),
            self.service("mailpit", 1025),
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "pretix-config"},
                "data": self.config_files(),
            },
            self.deployment(
                "postgres",
                self.pod_template(
                    "postgres",
                    999,
                    [
                        self.container(
                            "postgres",
                            POSTGRES_IMAGE,
                            command=["docker-entrypoint.sh", "postgres", "-c", "max_connections=300"],
                            env=[
                                {"name": "POSTGRES_DB", "value": "pretix"},
                                {"name": "POSTGRES_USER", "value": "pretix"},
                                {"name": "POSTGRES_PASSWORD_FILE", "value": "/run/pretix-credentials/db-password"},
                                {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                            ],
                            volumeMounts=[
                                {"name": "postgres-data", "mountPath": "/var/lib/postgresql/data"},
                                {"name": "system-password", "mountPath": "/run/pretix-credentials", "readOnly": True},
                            ],
                            readinessProbe={
                                "exec": {"command": ["pg_isready", "-h", "127.0.0.1", "-U", "pretix", "-d", "pretix"]},
                                "periodSeconds": 5,
                                "failureThreshold": 4,
                            },
                            resources={"requests": {"memory": "256Mi", "cpu": "300m"}, "limits": {"memory": "2Gi"}},
                        )
                    ],
                    [{"name": "postgres-data", "persistentVolumeClaim": {"claimName": "postgres-data"}}, secret_vol],
                ),
            ),
            self.deployment(
                "redis",
                self.pod_template(
                    "redis",
                    999,
                    [
                        self.container(
                            "redis",
                            REDIS_IMAGE,
                            command=["redis-server", "--appendonly", "yes", "--dir", "/data"],
                            volumeMounts=[{"name": "redis-data", "mountPath": "/data"}],
                            readinessProbe={"exec": {"command": ["redis-cli", "ping"]}, "periodSeconds": 5},
                            resources={"requests": {"memory": "128Mi", "cpu": "150m"}, "limits": {"memory": "1Gi"}},
                        )
                    ],
                    [{"name": "redis-data", "persistentVolumeClaim": {"claimName": "redis-data"}}],
                ),
            ),
            self.deployment(
                "mailpit",
                self.pod_template(
                    "mailpit",
                    10001,
                    [
                        self.container(
                            "mailpit",
                            MAILPIT_IMAGE,
                            env=[
                                {"name": "MP_DATABASE", "value": "/data/mailpit.db"},
                                {"name": "MP_SMTP_BIND_ADDR", "value": "0.0.0.0:1025"},
                                {"name": "MP_UI_BIND_ADDR", "value": "127.0.0.1:8025"},
                            ],
                            volumeMounts=[{"name": "mailpit-data", "mountPath": "/data"}],
                            readinessProbe={"tcpSocket": {"port": 1025}, "periodSeconds": 5},
                            resources={"requests": {"memory": "64Mi", "cpu": "100m"}, "limits": {"memory": "512Mi"}},
                        )
                    ],
                    [{"name": "mailpit-data", "persistentVolumeClaim": {"claimName": "mailpit-data"}}],
                ),
            ),
        ]

    def migration_document(self):
        container = self.container(
            "migrate",
            PRETIX_IMAGE,
            command=["pretix", "migrate", "--noinput"],
            env=self.app_env(),
            volumeMounts=self.app_mounts(),
            resources={"requests": {"memory": "256Mi", "cpu": "250m"}, "limits": {"memory": "2Gi"}},
        )
        template = self.pod_template("pretix-migrate", UID, [container], self.app_volumes())
        template["spec"]["restartPolicy"] = "Never"
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "pretix-migrate"},
            "spec": {"backoffLimit": 1, "template": template},
        }

    def app_documents(self):
        probe = {
            "httpGet": {"path": "/healthcheck/", "port": 8080, "httpHeaders": [{"name": "Host", "value": "pretix-web"}]}
        }
        web = self.container(
            "web",
            PRETIX_IMAGE,
            command=["/usr/bin/supervisord", "-n", "-c", "/etc/pretix/supervisor.conf"],
            env=self.app_env(),
            volumeMounts=self.app_mounts(),
            ports=[{"containerPort": 8080, "name": "http"}],
            resources={"requests": {"memory": "1Gi", "cpu": "1500m"}, "limits": {"memory": "9Gi"}},
            readinessProbe={**probe, "periodSeconds": 8, "timeoutSeconds": 8, "failureThreshold": 4},
            startupProbe={**probe, "periodSeconds": 10, "timeoutSeconds": 8, "failureThreshold": 90},
        )
        worker = self.container(
            "worker",
            PRETIX_IMAGE,
            command=["pretix", "taskworker", "--concurrency=4", "--loglevel=INFO"],
            env=self.app_env(),
            volumeMounts=self.app_mounts(),
            resources={"requests": {"memory": "512Mi", "cpu": "500m"}, "limits": {"memory": "3Gi"}},
            readinessProbe={
                "exec": {
                    "command": ["python", "-c", 'import socket; socket.create_connection(("redis",6379),2).close()']
                },
                "periodSeconds": 10,
            },
        )
        return [
            self.service("pretix-web", 80, 8080, node_port=True),
            self.deployment("pretix-web", self.pod_template("pretix-web", UID, [web], self.app_volumes())),
            self.deployment("pretix-worker", self.pod_template("pretix-worker", UID, [worker], self.app_volumes())),
        ]

    def cron_document(self):
        container = self.container(
            "periodic",
            PRETIX_IMAGE,
            command=["pretix", "cron"],
            env=self.app_env(),
            volumeMounts=self.app_mounts(),
            resources={"requests": {"memory": "256Mi", "cpu": "100m"}, "limits": {"memory": "2Gi"}},
        )
        template = self.pod_template("pretix-periodic", UID, [container], self.app_volumes())
        template["spec"]["restartPolicy"] = "Never"
        return {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "pretix-periodic"},
            "spec": {
                "schedule": "* * * * *",
                "concurrencyPolicy": "Forbid",
                "successfulJobsHistoryLimit": 1,
                "failedJobsHistoryLimit": 1,
                "jobTemplate": {"spec": {"backoffLimit": 1, "template": template}},
            },
        }

    def scanner_documents(self):
        items = {w["list"]: w["item"] for w in self.catalog["workshops"]}
        rooms = [
            {
                "list": lst["id"],
                "room": lst["room"],
                "workshop": lst["workshop"],
                "seats": lst["seats"],
                "item": items.get(lst["id"]),
            }
            for lst in self.catalog["rooms"]
        ]
        config = {
            "api": "http://pretix-web",
            "organizer": ORGANIZER,
            "event": EVENT,
            "entrance": self.catalog["entrance_list"],
            "rooms": rooms,
        }
        if not self.profile["scanner_walk_in"]:
            config["walk_in"] = False
        if self.profile["scanner_handheld"]:
            config["handheld_port"] = HANDHELD_PORT
        if self.profile["scanner_waitlist_watch"]:
            config["waitlist_watch"] = True
        container = self.container(
            "door-scanner",
            PYTHON_IMAGE,
            command=["python", "-u", "/opt/door/door_scanner.py"],
            env=[
                {"name": "SCANNER_CONFIG", "value": "/opt/door/stations.json"},
                {
                    "name": "PRETIX_TOKEN",
                    "valueFrom": {"secretKeyRef": {"name": "door-scanner-token", "key": "token"}},
                },
            ],
            volumeMounts=[{"name": "door", "mountPath": "/opt/door", "readOnly": True}],
            resources={"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
        )
        extra = []
        if self.profile["scanner_handheld"]:
            container["ports"] = [{"containerPort": HANDHELD_PORT, "name": "readers"}]
            container["readinessProbe"] = {"tcpSocket": {"port": HANDHELD_PORT}, "periodSeconds": 10}
            extra.append(self.service("door-scanner", HANDHELD_PORT, node_port=True))
        return extra + [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "door-scanner"},
                "data": {
                    "door_scanner.py": (RUNTIME / "door_scanner.py").read_text(),
                    "stations.json": json.dumps(config, indent=2) + "\n",
                },
            },
            self.deployment(
                "door-scanner",
                self.pod_template(
                    "door-scanner", 1000, [container], [{"name": "door", "configMap": {"name": "door-scanner"}}]
                ),
            ),
        ]

    # ------------------------------------------------------------------ lifecycle
    def ensure_secret(self):
        if self.command("get", "secret", "pretix-system", "--ignore-not-found", "-o", "name").strip():
            return
        self.command(
            "create", "secret", "generic", "pretix-system", f"--from-literal=db-password={secrets.token_urlsafe(32)}"
        )

    def rollout(self, *names, timeout=None):
        timeout = timeout or self.startup_timeout
        for name in names:
            self.command("rollout", "status", f"deployment/{name}", f"--timeout={timeout}s", timeout=timeout + 30)

    def wait_database(self, timeout=360):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.psql("SELECT 1").strip() == "1":
                    return
            except subprocess.CalledProcessError:
                pass
            time.sleep(3)
        raise RuntimeError("PostgreSQL did not accept queries")

    def deploy(self):
        self.create_namespace()
        self.ensure_secret()
        self.apply(self.stores_documents())
        self.rollout("postgres", "redis", "mailpit")
        self.wait_database()
        self.command("delete", "job", "pretix-migrate", "--ignore-not-found", "--wait=true")
        self.apply([self.migration_document()])
        self.command("wait", "--for=condition=complete", "job/pretix-migrate", "--timeout=900s", timeout=930)
        self.apply(self.app_documents())
        self.rollout("pretix-web", "pretix-worker")
        self.catalog = {"organizer": ORGANIZER, "event": EVENT, **self.bootstrap()}
        self.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "pretix-api"},
                    "type": "Opaque",
                    "stringData": {"token": self.catalog.pop("organizer_token")},
                },
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "door-scanner-token"},
                    "type": "Opaque",
                    "stringData": {"token": self.catalog.pop("scanner_token")},
                },
            ]
        )
        self.apply([self.cron_document()])
        self.apply(self.scanner_documents())
        self.rollout("door-scanner", timeout=600)

    def bootstrap(self):
        start = datetime.now(UTC).replace(second=0, microsecond=0)
        arguments = {
            "organizer": ORGANIZER,
            "event": EVENT,
            "workshops": WORKSHOPS,
            "date_from": (start + timedelta(hours=3)).isoformat(),
            "date_to": (start + timedelta(hours=11)).isoformat(),
            "presale_start": (start - timedelta(days=21)).isoformat(),
            "admin_password": secrets.token_urlsafe(18),
            "ticket_download": self.profile["ticket_download"],
            "close_when_sold_out": self.profile["close_when_sold_out"],
            "reservation_minutes": self.profile["reservation_minutes"],
        }
        return self.pretix_python((RUNTIME / "bootstrap.py").read_text(), arguments)

    def seed_history(self, fills, seed=None):
        """Registrations that came in through the shop over the presale period (see runtime/history.py).

        ``fills`` maps workshop item ids to the number of registrations to create.
        """
        arguments = {
            "organizer": ORGANIZER,
            "event": EVENT,
            "fills": {str(k): v for k, v in fills.items()},
            "seed": seed if seed is not None else secrets.randbits(32),
        }
        return self.pretix_python((RUNTIME / "history.py").read_text(), arguments, timeout=600)

    def node_url(self, service):
        """Base URL of a NodePort service, as reached from the conductor host."""
        svc = json.loads(self.command("get", "svc", service, "-o", "json"))
        port = svc["spec"]["ports"][0]["nodePort"]
        nodes = json.loads(self.command("get", "nodes", "-o", "json"))["items"]
        for node in nodes:
            if "node-role.kubernetes.io/control-plane" in node["metadata"].get("labels", {}):
                continue
            for addr in node["status"]["addresses"]:
                if addr["type"] == "InternalIP":
                    return f"http://{addr['address']}:{port}"
        raise RuntimeError(f"No worker node address for the {service} NodePort")

    def pretix_python(self, source, arguments=None, timeout=300):
        """Run a Django script inside pretix-web (on stdin, never written to disk); returns its JSON output."""
        program = (
            "import os, json, sys\nos.chdir('/pretix/src')\nsys.path.insert(0, '/pretix/src')\n"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'production_settings')\n"
            "os.environ.setdefault('DATA_DIR', '/data/')\n"
            "import django\ndjango.setup()\n"
            f"ARGS = json.loads({json.dumps(json.dumps(arguments or {}))})\n" + source
        )
        out = self.command(
            "exec", "-i", "deploy/pretix-web", "-c", "web", "--", "python", "-", input_text=program, timeout=timeout
        )
        return json.loads(out.strip().splitlines()[-1])

    def psql(self, sql, timeout=60):
        script = (
            'export PGPASSWORD="$(cat /run/pretix-credentials/db-password)"; '
            'exec psql -h 127.0.0.1 -U pretix -d pretix -v ON_ERROR_STOP=1 -tAc "$1"'
        )
        return self.command("exec", "deploy/postgres", "--", "sh", "-c", script, "--", sql, timeout=timeout)

    def front_door(self):
        """Base URL of the NodePort front door, as reached from the conductor host."""
        return self.node_url("pretix-web")

    def mailbox(self, query, limit=50):
        """Search the Mailpit sink (its API listens on the pod's loopback only)."""
        from urllib.parse import quote

        out = self.command(
            "exec",
            "deploy/mailpit",
            "--",
            "wget",
            "-qO-",
            f"http://127.0.0.1:8025/api/v1/search?limit={limit}&query={quote(query)}",
            timeout=60,
        )
        return json.loads(out)

    def mail_text(self, message_id):
        return self.command(
            "exec", "deploy/mailpit", "--", "wget", "-qO-", f"http://127.0.0.1:8025/view/{message_id}.txt", timeout=60
        )

    def create_workload(self, **kwargs):
        pass

    def start_workload(self):
        pass

    def stop_workload(self):
        pass

    def cleanup(self):
        self.kubectl.delete_namespace(self.namespace)

    def delete(self):
        self.cleanup()

    def pods(self, app):
        return json.loads(self.command("get", "pods", "-l", f"app={app}", "-o", "json"))["items"]

    def logs(self, target, since=None, tail=None, container=None):
        args = ["logs", target]
        if container:
            args += ["-c", container]
        if since:
            args.append(f"--since-time={since}")
        if tail:
            args.append(f"--tail={tail}")
        return self.command(*args, timeout=120)
