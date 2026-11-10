"""Apache Airflow 3.3.2, deployed from the official Airflow Helm chart (CeleryExecutor).

The chart (1.22.0, pinned to the apache/airflow 3.3.2 image) is vendored next to
this file and checked against its SHA-256 before installation. The metadata
database is an external PostgreSQL StatefulSet reached through PgBouncer with the
chart's default transaction pooling (no ``idle_transaction_timeout``). Two small
operator DAGs are mounted from a ConfigMap, and an ``airflow-monitor`` deployment
watches that the minutely heartbeat pipeline keeps completing.

``AirflowApplication(commerce=True)`` additionally runs the shop pipelines on the
same Airflow: a ``storefront`` that records orders in a ``shop`` database and
publishes them to an in-cluster SQS queue (ElasticMQ, ``orders-sqs``), the
continuous ``order_intake`` DAG (a deferrable ``SqsSensor`` plus a shipping task),
and the ``partner_settlement`` DAG whose deadline alert posts late notices to
``ops-notify``. The monitor then also reconciles orders against shipments and late
settlement runs against late notices.

``AirflowApplication(commerce=True, manifests=True)`` also runs the
``carrier_manifest`` DAG: every five minutes it puts the new shipments on a
carrier manifest, and when the run succeeds its DAG-level success callback
requests the carrier pickup through ``ops-notify``. The monitor then also checks
that every closed manifest got its pickup request.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"

AIRFLOW_IMAGE = "apache/airflow:3.3.2"
POSTGRES_IMAGE = "postgres:16.4"
ELASTICMQ_IMAGE = "softwaremill/elasticmq-native:1.6.14"
DAGS = ("fleet_heartbeat.py", "fleet_backlog.py")
COMMERCE_DAGS = ("order_intake.py", "partner_settlement.py", "ops_alerts.py")
MANIFEST_DAGS = ("carrier_manifest.py", "carrier_desk.py")
#: DAG-folder modules that hold callbacks, not DAGs.
HELPER_MODULES = ("ops_alerts.py", "carrier_desk.py")
DB_URL = "postgresql://airflow:airflow@postgres:5432/airflow"
SHOP_DB_URL = "postgresql://shop:shop@postgres:5432/shop"
ORDERS_QUEUE_URL = "http://orders-sqs:9324/000000000000/orders"

COMMERCE_DESC = (
    " The shop pipelines run on the same Airflow: a `storefront` deployment records customer orders in the `shop` "
    "PostgreSQL database (same `postgres` server) and publishes each order to the `orders` queue of an in-cluster "
    "SQS service (`orders-sqs`, ElasticMQ); the continuous `order_intake` DAG waits for orders on that queue and "
    "records a shipment for each; the `partner_settlement` DAG reports shipments to the payment partner every five "
    "minutes and, when a run is late, posts a late notice to the partner-settlement channel through the "
    "`ops-notify` relay (notices are stored in the shop database). The `airflow-monitor` also reconciles the shop: "
    "orders against shipments and late settlement runs against late notices."
)
MANIFEST_DESC = (
    " Every five minutes the `carrier_manifest` DAG puts the newly shipped parcels on a carrier manifest (`manifests` "
    "and `manifest_lines` in the shop database) and requests the carrier pickup for it through `ops-notify` "
    "(carrier-pickups channel); the carrier collects only manifests it received a pickup request for. The "
    "`airflow-monitor` also checks that every closed manifest got its pickup request."
)


class AirflowApplication(KubernetesCommands, Application):
    app_id = "airflow"
    metadata_file = HERE / "metadata.json"
    CHART = HERE / "airflow-1.22.0.tgz"
    CHART_SHA256 = "1f7d1dfe3d58e2c54899950aba907a43625a18c8b6fb3c54760c21592129a5b6"
    VALUES = HERE / "values.yaml"
    VALUES_COMMERCE = HERE / "values-commerce.yaml"
    VALUES_MANIFESTS = HERE / "values-manifests.yaml"
    RELEASE = "airflow"
    READY_TIMEOUT_S = 1500
    frontend_port = 8080

    SCHEDULER_DEPLOY = "airflow-scheduler"
    POSTGRES_POD = "postgres-0"
    #: Deployments/StatefulSets whose replica count is part of the healthy topology.
    topology = {
        "airflow-scheduler": 1,
        "airflow-dag-processor": 1,
        "airflow-api-server": 1,
        "airflow-monitor": 1,
    }

    def __init__(self, commerce: bool = False, manifests: bool = False):
        super().__init__(self.metadata_file)
        self.load_app_json()
        self.commerce = commerce or manifests
        self.manifests = manifests
        commerce = self.commerce
        self.app_name = self.name
        self.description = (
            self.get_app_json()["Desc"] + (COMMERCE_DESC if commerce else "") + (MANIFEST_DESC if manifests else "")
        )
        self.base_description = self.description
        if commerce:
            self.topology = {**self.topology, "storefront": 1, "ops-notify": 1, "orders-sqs": 1}
        self.dag_files = DAGS + (COMMERCE_DAGS if commerce else ()) + (MANIFEST_DAGS if manifests else ())
        self.dag_ids = [name[:-3] for name in self.dag_files if name not in HELPER_MODULES]
        self.kubectl = KubeCtl()
        self.frontend_service = "airflow-api-server"
        self.helm_deploy = True

    # ------------------------------------------------------------------ manifests
    def postgres_documents(self):
        return [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "postgres"},
                "spec": {"selector": {"app": "postgres"}, "ports": [{"name": "db", "port": 5432}]},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": "postgres"},
                "spec": {
                    "serviceName": "postgres",
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "postgres"}},
                    "template": {
                        "metadata": {"labels": {"app": "postgres"}},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "enableServiceLinks": False,
                            "containers": [
                                {
                                    "name": "postgres",
                                    "image": POSTGRES_IMAGE,
                                    "imagePullPolicy": "IfNotPresent",
                                    "args": ["-c", "max_connections=200"],
                                    "env": [
                                        {"name": "POSTGRES_USER", "value": "airflow"},
                                        {"name": "POSTGRES_DB", "value": "airflow"},
                                        {"name": "POSTGRES_PASSWORD", "value": "airflow"},
                                        {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                                    ],
                                    "ports": [{"name": "db", "containerPort": 5432}],
                                    "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}],
                                    "readinessProbe": {
                                        "exec": {"command": ["pg_isready", "-U", "airflow", "-d", "airflow"]},
                                        "periodSeconds": 5,
                                    },
                                    "resources": {
                                        "requests": {"cpu": "250m", "memory": "512Mi"},
                                        "limits": {"memory": "2Gi"},
                                    },
                                }
                            ],
                        },
                    },
                    "volumeClaimTemplates": [
                        {
                            "metadata": {"name": "data"},
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "storageClassName": "standard",
                                "resources": {"requests": {"storage": "4Gi"}},
                            },
                        }
                    ],
                },
            },
        ]

    def dags_configmap(self):
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "operator-dags"},
            "data": {name: (RUNTIME / name).read_text() for name in self.dag_files},
        }

    def monitor_configmap(self):
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "monitor-src"},
            "data": {"monitor.py": (RUNTIME / "monitor.py").read_text()},
        }

    def monitor_documents(self):
        return [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "airflow-monitor"},
                "spec": {
                    "selector": {"app": "airflow-monitor"},
                    "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "airflow-monitor", "labels": {"app": "airflow-monitor"}},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "airflow-monitor"}},
                    "template": {
                        "metadata": {"labels": {"app": "airflow-monitor"}},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "terminationGracePeriodSeconds": 5,
                            "containers": [
                                {
                                    "name": "monitor",
                                    "image": AIRFLOW_IMAGE,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["python", "-u", "/opt/monitor/monitor.py"],
                                    "env": [{"name": "MONITOR_DB_URL", "value": DB_URL}]
                                    + ([{"name": "MONITOR_SHOP_DB_URL", "value": SHOP_DB_URL}] if self.commerce else [])
                                    + ([{"name": "MONITOR_MANIFESTS", "value": "1"}] if self.manifests else []),
                                    "ports": [{"containerPort": 8080}],
                                    "readinessProbe": {
                                        "httpGet": {"path": "/status", "port": 8080},
                                        "periodSeconds": 10,
                                    },
                                    "volumeMounts": [{"name": "src", "mountPath": "/opt/monitor", "readOnly": True}],
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": "256Mi"},
                                        "limits": {"memory": "512Mi"},
                                    },
                                }
                            ],
                            "volumes": [{"name": "src", "configMap": {"name": "monitor-src"}}],
                        },
                    },
                },
            },
        ]

    # ------------------------------------------------------------------ shop
    @staticmethod
    def _service(name: str, port: int, target: int | None = None, extra_ports=()) -> dict:
        ports = [{"name": "http", "port": port, "targetPort": target or port}, *extra_ports]
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {"selector": {"app": name}, "ports": ports},
        }

    @staticmethod
    def _python_service(name: str, script: str, env: list[dict]) -> list[dict]:
        """A small Python HTTP service run from the Airflow image (boto3/SQLAlchemy are already in it)."""
        return [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": f"{name}-src"},
                "data": {script: (RUNTIME / script).read_text()},
            },
            AirflowApplication._service(name, 8080),
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "labels": {"app": name}},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": name}},
                    "template": {
                        "metadata": {"labels": {"app": name}},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "terminationGracePeriodSeconds": 10,
                            "containers": [
                                {
                                    "name": name,
                                    "image": AIRFLOW_IMAGE,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["python", "-u", f"/opt/{name}/{script}"],
                                    "env": env,
                                    "ports": [{"containerPort": 8080}],
                                    "readinessProbe": {
                                        "httpGet": {"path": "/healthz", "port": 8080},
                                        "periodSeconds": 10,
                                    },
                                    "volumeMounts": [{"name": "src", "mountPath": f"/opt/{name}", "readOnly": True}],
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": "192Mi"},
                                        "limits": {"memory": "512Mi"},
                                    },
                                }
                            ],
                            "volumes": [{"name": "src", "configMap": {"name": f"{name}-src"}}],
                        },
                    },
                },
            },
        ]

    def orders_sqs_documents(self) -> list[dict]:
        return [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "orders-sqs-config"},
                "data": {"elasticmq.conf": (RUNTIME / "elasticmq.conf").read_text()},
            },
            self._service("orders-sqs", 9324, extra_ports=[{"name": "stats", "port": 9325, "targetPort": 9325}]),
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "orders-sqs", "labels": {"app": "orders-sqs"}},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "orders-sqs"}},
                    "template": {
                        "metadata": {"labels": {"app": "orders-sqs"}},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "containers": [
                                {
                                    "name": "elasticmq",
                                    "image": ELASTICMQ_IMAGE,
                                    "imagePullPolicy": "IfNotPresent",
                                    "ports": [{"containerPort": 9324}, {"containerPort": 9325}],
                                    "readinessProbe": {"tcpSocket": {"port": 9324}, "periodSeconds": 5},
                                    "volumeMounts": [
                                        {
                                            "name": "config",
                                            "mountPath": "/opt/elasticmq.conf",
                                            "subPath": "elasticmq.conf",
                                            "readOnly": True,
                                        }
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": "128Mi"},
                                        "limits": {"memory": "512Mi"},
                                    },
                                }
                            ],
                            "volumes": [{"name": "config", "configMap": {"name": "orders-sqs-config"}}],
                        },
                    },
                },
            },
        ]

    def shop_service_documents(self) -> list[dict]:
        aws_env = [
            {"name": "AWS_ACCESS_KEY_ID", "value": "storefront"},
            {"name": "AWS_SECRET_ACCESS_KEY", "value": "storefront-secret"},
            {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"},
        ]
        return self._python_service(
            "ops-notify", "ops_notify.py", [{"name": "SHOP_DB_URL", "value": SHOP_DB_URL}]
        ) + self._python_service(
            "storefront",
            "storefront.py",
            [
                {"name": "SHOP_DB_URL", "value": SHOP_DB_URL},
                {"name": "ORDERS_QUEUE_URL", "value": ORDERS_QUEUE_URL},
                {"name": "SQS_ENDPOINT_URL", "value": "http://orders-sqs:9324"},
                *aws_env,
            ],
        )

    def bootstrap_shop_database(self) -> None:
        exists = self.psql("SELECT 1 FROM pg_roles WHERE rolname = 'shop'").strip()
        if not exists:
            self.psql("CREATE ROLE shop LOGIN PASSWORD 'shop'")
        if not self.psql("SELECT 1 FROM pg_database WHERE datname = 'shop'").strip():
            self.psql("CREATE DATABASE shop OWNER shop")
        self.command(
            "exec",
            "-i",
            self.POSTGRES_POD,
            "--",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "shop",
            "-d",
            "shop",
            "-q",
            input_text=(RUNTIME / "shop_schema.sql").read_text()
            + ((RUNTIME / "manifest_schema.sql").read_text() if self.manifests else ""),
            timeout=120,
        )

    def shop_psql(self, sql: str, timeout: int = 60) -> str:
        """Run SQL against the shop database; unaligned, tuples only."""
        return self.command(
            "exec", self.POSTGRES_POD, "--", "psql", "-U", "shop", "-d", "shop", "-tA", "-c", sql, timeout=timeout
        )

    # ------------------------------------------------------------------ deployment
    def deploy(self):
        digest = hashlib.sha256(self.CHART.read_bytes()).hexdigest()
        if digest != self.CHART_SHA256:
            raise RuntimeError(f"Vendored chart {self.CHART.name} has digest {digest}, expected {self.CHART_SHA256}")
        self.kubectl.create_namespace_if_not_exist(self.namespace)
        self.apply(self.postgres_documents())
        self.command("rollout", "status", "statefulset/postgres", "--timeout=300s", timeout=330)
        if self.commerce:
            self.wait_postgres_accepting()
            self.bootstrap_shop_database()
            self.apply(self.orders_sqs_documents())
        self.apply([self.dags_configmap(), self.monitor_configmap()])
        if not Helm.exists_release(self.RELEASE, self.namespace):
            values = (
                ["-f", str(self.VALUES)]
                + (["-f", str(self.VALUES_COMMERCE)] if self.commerce else [])
                + (["-f", str(self.VALUES_MANIFESTS)] if self.manifests else [])
            )
            Helm.install(
                release_name=self.RELEASE,
                chart_path=str(self.CHART),
                namespace=self.namespace,
                remote_chart=True,
                extra_args=[*values, "--timeout", "20m"],
            )
        self.apply(self.monitor_documents())
        if self.commerce:
            self.apply(self.shop_service_documents())
        self.kubectl.wait_for_ready(self.namespace, max_wait=self.READY_TIMEOUT_S)
        self.wait_dags_parsed()

    def wait_postgres_accepting(self, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.psql("SELECT 1")
                return
            except subprocess.CalledProcessError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(3)

    def wait_dags_parsed(self, timeout=600):
        """Every operator DAG registered and unpaused so the scheduler runs it."""
        deadline = time.monotonic() + timeout
        last = ""
        wanted = ", ".join(f"'{dag_id}'" for dag_id in self.dag_ids)
        while time.monotonic() < deadline:
            rows = self.psql(f"SELECT dag_id FROM dag WHERE dag_id IN ({wanted})").split()
            if all(dag_id in rows for dag_id in self.dag_ids):
                for dag_id in self.dag_ids:
                    self.command(
                        "exec",
                        f"deploy/{self.SCHEDULER_DEPLOY}",
                        "--",
                        "airflow",
                        "dags",
                        "unpause",
                        dag_id,
                        timeout=120,
                    )
                return
            last = rows
            time.sleep(10)
        raise RuntimeError(f"Operator DAGs did not register: {last}")

    # ------------------------------------------------------------------ workload
    def create_workload(self, **kwargs):
        pass

    def start_workload(self):
        # The minutely fleet_heartbeat schedule is the steady-state workload; it
        # starts as soon as the scheduler is running.
        pass

    def stop_workload(self):
        pass

    # ------------------------------------------------------------------ helpers
    def psql(self, sql: str, timeout: int = 60) -> str:
        """Run SQL in the postgres pod; unaligned, tuples only."""
        return self.command(
            "exec",
            self.POSTGRES_POD,
            "--",
            "psql",
            "-U",
            "airflow",
            "-d",
            "airflow",
            "-tA",
            "-c",
            sql,
            timeout=timeout,
        )

    def monitor_status(self) -> dict:
        out = self.command(
            "exec",
            "deploy/airflow-monitor",
            "--",
            "python",
            "-c",
            "import urllib.request,sys;sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8080/status').read().decode())",
            timeout=60,
        )
        return json.loads(out)

    def scheduler_pod(self) -> str:
        pods = json.loads(
            self.command("get", "pods", "-l", "component=scheduler,release=airflow", "-o", "json", timeout=30)
        )["items"]
        running = [
            p["metadata"]["name"]
            for p in pods
            if p.get("status", {}).get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")
        ]
        if not running:
            raise RuntimeError("No running scheduler pod")
        return running[0]

    def node_container(self, pod: str) -> str:
        """kind node (docker container) hosting ``pod``."""
        node = self.command("get", "pod", pod, "-o", "jsonpath={.spec.nodeName}", timeout=30).strip()
        provider = json.loads(self.command("get", "node", node, "-o", "json", timeout=30))["spec"].get("providerID", "")
        if not provider.startswith("kind://"):
            raise RuntimeError("This app's fault injection freezes/partitions a pod from its node (kind only)")
        return provider.rsplit("/", 1)[-1]

    # ------------------------------------------------------------------ teardown
    def cleanup(self):
        # Force-delete pods (no graceful wait) so a hung volume unmount does not
        # block namespace teardown between runs.
        subprocess.run(
            ["kubectl", "-n", self.namespace, "delete", "pods", "--all", "--grace-period=0", "--force", "--wait=false"],
            capture_output=True,
            timeout=120,
        )
        self.kubectl.delete_namespace(self.namespace)

    def delete(self):
        self.cleanup()
