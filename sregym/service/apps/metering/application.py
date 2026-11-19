"""A metered-API company's usage platform on PostgreSQL logical replication.

Core holds usage events. Billing subscribes to all of them and rates every change
into its ledger; finance (a database on core) subscribes to the ledger and
invoices from it. Each enterprise customer's own database subscribes to a
row-filtered usage publication and a corrections publication. The customer
databases, the API gateways that send meter records, and the checks live in the
hidden namespace.
"""

import base64
import contextlib
import json
import secrets
import subprocess
import time
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.kubectl import KubeCtl

HERE = Path(__file__).parent
COMPANY = HERE / "company"
HIDDEN = "khaos"
OLD_IMAGE = "postgres:17.4"
NEW_IMAGE = "postgres:17.6"
CUSTOMER_IMAGE = "postgres:17.6-bookworm"
STORAGE_CLASS = "openebs-hostpath"
PRICES = {"api_calls": "0.002", "tokens_k": "0.0150", "storage_gb_h": "0.0400"}

#: tenant -> (name, plan, weight in gateway traffic, billing day)
TENANTS = {
    "pell": ("Pell Analytics", "enterprise", 3, 3),
    "quill": ("Quill Health", "enterprise", 2, 7),
    "larkspur": ("Larkspur Logistics", "enterprise", 3, 11),
    "ostrava": ("Ostrava Mobility", "enterprise", 2, 15),
    "vale": ("Vale Robotics", "enterprise", 1, 19),
    "arden": ("Arden Media", "growth", 2, 4),
    "brisk": ("Brisk Payments", "growth", 1, 8),
    "cinder": ("Cinder Games", "growth", 2, 12),
    "dorian": ("Dorian Legal", "growth", 1, 16),
    "ember": ("Ember Retail", "starter", 1, 20),
    "fallow": ("Fallow Farms", "starter", 1, 24),
    "garnet": ("Garnet Travel", "growth", 2, 6),
    "hollis": ("Hollis Insurance", "growth", 1, 10),
    "ionic": ("Ionic Labs", "starter", 1, 14),
}
#: customer database -> tenant; each has its own subscription and login on core.
CUSTOMER_DATABASES = {
    "pell_usage": "pell",
    "quill_usage": "quill",
    "larkspur_live": "larkspur",
    "larkspur_monthend": "larkspur",
    "ostrava_usage": "ostrava",
    "vale_usage": "vale",
}
RECEIPT_DATABASE = "larkspur_monthend"
STREAMED = sorted(set(CUSTOMER_DATABASES.values()))
SUBSCRIBERS = "subscribers"
GATEWAY = "gateway"


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def publications_conf(revision, comment, operations):
    """The declared publication set; operations maps publication -> publish list."""
    lines = [f"# revision: {revision} {comment}", "# publication | table | row filter | publish"]
    lines.append(f"billing_usage | usage_events | | {operations.get('billing_usage', 'insert,update,delete')}")
    for tenant in operations.get("tenants", STREAMED):
        usage, corr = operations.get(tenant, ("insert,update,delete", ""))
        lines.append(f"usage_{tenant} | usage_events | tenant_id = '{tenant}' | {usage}")
        lines.append(f"corr_{tenant} | usage_events | tenant_id = '{tenant}' | {corr}")
    return "\n".join(lines) + "\n"


def ledger_conf(revision, comment, ledger_all, ledger_corr):
    return (
        f"# revision: {revision} {comment}\n# publication | table | row filter | publish\n"
        f"ledger_all | ledger_lines | | {ledger_all}\nledger_corr | ledger_lines | | {ledger_corr}\n"
    )


class MeteringApplication(KubernetesCommands, Application):
    def __init__(self):
        super().__init__(HERE / "metadata.json")
        self.load_app_json()
        self.app_name = self.name
        self.description = self.get_app_json()["Desc"]
        self.kubectl = KubeCtl()
        self.helm_deploy = False
        self.frontend_service = None

    # ---------------------------------------------------------------- transport
    def hidden(self, *args, input_text=None, timeout=120):
        return subprocess.run(
            ["kubectl", "-n", HIDDEN, *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout

    def psql(self, pod, database, script, hidden=False, timeout=300):
        run = self.hidden if hidden else self.command
        args = [
            "exec",
            "-i",
            pod,
            "-c",
            "postgres",
            "--",
            "psql",
            "-XAtq",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            database,
            "-f",
            "-",
        ]
        return run(*args, input_text=script, timeout=timeout)

    def core(self, script, database="usage", timeout=300):
        return self.psql("core-0", database, script, timeout=timeout)

    def billing(self, script, timeout=300):
        return self.psql("billing-0", "billing", script, timeout=timeout)

    def finance(self, script, timeout=300):
        return self.core(script, database="finance", timeout=timeout)

    def customer(self, database, script, timeout=300):
        return self.psql(f"{SUBSCRIBERS}-0", database, script, hidden=True, timeout=timeout)

    def rows(self, run, script):
        """Run a query that returns one JSON document per row."""
        return [json.loads(line) for line in run(script).splitlines() if line.strip()]

    def secret(self, name, key, hidden=False):
        run = self.hidden if hidden else self.command
        data = json.loads(run("get", "secret", name, "-o", "json"))["data"]
        return base64.b64decode(data[key]).decode()

    def apply_documents(self, documents, hidden=False):
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": documents})
        (self.hidden if hidden else self.command)("apply", "-f", "-", input_text=body, timeout=180)

    # ---------------------------------------------------------------- manifests
    @staticmethod
    def config_map(name, files):
        return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}, "data": files}

    @staticmethod
    def secret_document(name, data):
        return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "type": "Opaque", "stringData": data}

    @staticmethod
    def server_args(senders=20):
        return [
            "postgres",
            "-c",
            "wal_level=logical",
            "-c",
            f"max_wal_senders={senders}",
            "-c",
            f"max_replication_slots={senders}",
            "-c",
            "max_connections=200",
            "-c",
            "shared_buffers=192MB",
            "-c",
            "max_wal_size=2GB",
            "-c",
            "logging_collector=on",
            "-c",
            "log_directory=log",
            "-c",
            "log_filename=postgresql-%Y-%m-%d_%H%M%S.log",
            "-c",
            "log_rotation_size=64MB",
        ]

    @staticmethod
    def postgres_statefulset(name, image, args, secret, memory="1Gi", storage="6Gi", extra_containers=(), cpu=None):
        container = {
            "name": "postgres",
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "args": args,
            "env": [
                {"name": "POSTGRES_PASSWORD", "valueFrom": {"secretKeyRef": {"name": secret, "key": "password"}}},
                {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
            ],
            "ports": [{"containerPort": 5432, "name": "postgres"}],
            "readinessProbe": {
                "exec": {"command": ["pg_isready", "-U", "postgres", "-h", "127.0.0.1"]},
                "periodSeconds": 5,
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"memory": memory, **({"cpu": cpu} if cpu else {})},
            },
            "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}],
        }
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": name, "labels": {"app": name}},
            "spec": {
                "serviceName": name,
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"terminationGracePeriodSeconds": 60, "containers": [container, *extra_containers]},
                },
                "volumeClaimTemplates": [
                    {
                        "metadata": {"name": "data"},
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "storageClassName": STORAGE_CLASS,
                            "resources": {"requests": {"storage": storage}},
                        },
                    }
                ],
            },
        }

    @staticmethod
    def service(name, app=None):
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {"selector": {"app": app or name}, "ports": [{"port": 5432, "targetPort": 5432}]},
        }

    @staticmethod
    def client_env(host, database, user, secret, key="password"):
        return [
            {"name": "PGHOST", "value": host},
            {"name": "PGDATABASE", "value": database},
            {"name": "PGUSER", "value": user},
            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}},
        ]

    def ingest_deployment(self):
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "ingest", "labels": {"app": "ingest"}},
            "spec": {
                "serviceName": "ingest",
                "replicas": 2,
                "podManagementPolicy": "Parallel",
                "selector": {"matchLabels": {"app": "ingest"}},
                "template": {
                    "metadata": {"labels": {"app": "ingest"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "ingest",
                                "image": NEW_IMAGE,
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["bash", "/opt/ingest/ingest.sh"],
                                "env": self.client_env("core", "usage", "ingest", "ingest-db"),
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "64Mi"},
                                    "limits": {"memory": "256Mi"},
                                },
                                "volumeMounts": [{"name": "code", "mountPath": "/opt/ingest"}],
                            }
                        ],
                        "volumes": [{"name": "code", "configMap": {"name": "ingest"}}],
                    },
                },
            },
        }

    def job_pod(self, name, command, env, config_maps):
        return {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": name,
                    "image": NEW_IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "command": command,
                    "env": env,
                    "volumeMounts": [{"name": n, "mountPath": path} for n, path in config_maps.items()],
                }
            ],
            "volumes": [{"name": n, "configMap": {"name": n}} for n in config_maps],
        }

    def cronjob(self, name, schedule, script, host, database, secret, config_map, mount):
        return {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": name},
            "spec": {
                "schedule": schedule,
                "timeZone": "Etc/UTC",
                "concurrencyPolicy": "Forbid",
                "successfulJobsHistoryLimit": 3,
                "failedJobsHistoryLimit": 3,
                "jobTemplate": {
                    "spec": {
                        "backoffLimit": 0,
                        "template": {
                            "spec": self.job_pod(
                                name,
                                ["bash", f"{mount}/{script}"],
                                self.client_env(host, database, "postgres", secret),
                                {config_map: mount},
                            )
                        },
                    }
                },
            },
        }

    def pubsync_job(self, target, revision):
        host, database, secret = (
            ("core", "usage", "core-postgres") if target == "core" else ("billing", "billing", "billing-postgres")
        )
        pod = self.job_pod(
            "pubsync",
            ["bash", "/opt/pubsync/pubsync.sh", "/etc/pubsync/publications.conf"],
            self.client_env(host, database, "postgres", secret),
            {"pubsync": "/opt/pubsync", f"pubsync-{target}": "/etc/pubsync"},
        )
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": f"pubsync-{target}-{revision}", "labels": {"app": "pubsync", "target": target}},
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 600,
                "template": {"metadata": {"labels": {"app": "pubsync", "target": target}}, "spec": pod},
            },
        }

    def subscribers_statefulset(self):
        exporter = {
            "name": "exporter",
            "image": CUSTOMER_IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "command": [
                "bash",
                "-c",
                (
                    "while true; do psql -XAtq -h 127.0.0.1 -U postgres -d " + RECEIPT_DATABASE + " -c "
                    "\"update usage_events set export_receipt = 'EXP-' || upper(substr(md5(random()::text), 1, 10)), "
                    'exported_at = now() where export_receipt is null" >/dev/null 2>&1; sleep 15; done'
                ),
            ],
            "env": [{"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": "subscribers", "key": "password"}}}],
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
        }
        document = self.postgres_statefulset(
            SUBSCRIBERS,
            CUSTOMER_IMAGE,
            [
                "postgres",
                "-c",
                "max_logical_replication_workers=16",
                "-c",
                "max_worker_processes=24",
                "-c",
                "max_connections=100",
                "-c",
                "shared_buffers=128MB",
            ],
            "subscribers",
            memory="1Gi",
            storage="4Gi",
            extra_containers=[exporter],
        )
        return document

    def gateway_deployment(self):
        tenants = [t for t, spec in TENANTS.items() for _ in range(spec[2])]
        script = COMPANY.parent.joinpath("gateway.sh").read_text().replace("@TENANTS@", ",".join(tenants))
        return [
            self.config_map("gateway", {"gateway.sh": script}),
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": GATEWAY, "labels": {"app": GATEWAY}},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": GATEWAY}},
                    "template": {
                        "metadata": {"labels": {"app": GATEWAY}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "postgres",
                                    "image": CUSTOMER_IMAGE,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["bash", "/opt/gateway/gateway.sh"],
                                    "env": self.client_env(f"core.{self.namespace}.svc", "meter", "gateway", "gateway"),
                                    "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}},
                                    "volumeMounts": [
                                        {"name": "code", "mountPath": "/opt/gateway"},
                                        {"name": "state", "mountPath": "/run/load"},
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": "code", "configMap": {"name": "gateway"}},
                                {"name": "state", "emptyDir": {}},
                            ],
                        },
                    },
                },
            },
        ]

    @staticmethod
    def hidden_policy():
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "customer-sites"},
            "spec": {
                "podSelector": {
                    "matchExpressions": [{"key": "app", "operator": "In", "values": [SUBSCRIBERS, GATEWAY]}]
                },
                "policyTypes": ["Ingress"],
                "ingress": [
                    {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": HIDDEN}}}]}
                ],
            },
        }

    # ------------------------------------------------------------------- deploy
    def wait_rollout(self, kind, name, hidden=False, timeout=600):
        run = self.hidden if hidden else self.command
        run("rollout", "status", f"{kind}/{name}", f"--timeout={timeout}s", timeout=timeout + 30)

    def wait_pod_ready(self, pod, hidden=False, timeout=600):
        run = self.hidden if hidden else self.command
        deadline = time.monotonic() + timeout
        while True:
            with contextlib.suppress(subprocess.CalledProcessError):
                run("wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=30s", timeout=60)
                with contextlib.suppress(subprocess.CalledProcessError):
                    self.psql(pod, "postgres", "select 1;", hidden=hidden, timeout=30)
                    return
            if time.monotonic() > deadline:
                raise TimeoutError(f"{pod} not ready")
            time.sleep(3)

    def deploy(self):
        self.create_namespace()
        if not self.kubectl.exec_command(f"kubectl get namespace {HIDDEN} --ignore-not-found -o name").strip():
            self.kubectl.exec_command(f"kubectl create namespace {HIDDEN}")
        self.remove_hidden()
        self.passwords = {
            name: secrets.token_hex(16)
            for name in (
                "core",
                "billing",
                "ingest",
                "gateway",
                "billing_feed",
                "finance_feed",
                "subscribers",
                *(f"feed_{db}" for db in CUSTOMER_DATABASES),
            )
        }
        p = self.passwords
        self.apply_documents(
            [
                self.secret_document("core-postgres", {"password": p["core"]}),
                self.secret_document("billing-postgres", {"password": p["billing"]}),
                self.secret_document("ingest-db", {"password": p["ingest"]}),
                self.config_map("ingest", {"ingest.sh": (COMPANY / "ingest.sh").read_text()}),
                self.config_map("pubsync", {"pubsync.sh": (COMPANY / "pubsync.sh").read_text()}),
                self.config_map("finance-jobs", {"invoice-run.sh": (COMPANY / "invoice-run.sh").read_text()}),
                self.config_map("billing-jobs", {"rerate.sh": (COMPANY / "rerate.sh").read_text()}),
                self.postgres_statefulset("core", OLD_IMAGE, self.server_args(), "core-postgres", memory="1536Mi"),
                self.service("core"),
                self.postgres_statefulset("billing", OLD_IMAGE, self.server_args(), "billing-postgres", cpu="200m"),
                self.service("billing"),
            ]
        )
        self.apply_documents(
            [
                self.secret_document("subscribers", {"password": p["subscribers"]}),
                self.secret_document("gateway", {"password": p["gateway"]}),
                self.hidden_policy(),
                self.subscribers_statefulset(),
            ],
            hidden=True,
        )
        self.wait_pod_ready("core-0")
        self.wait_pod_ready("billing-0")
        self.wait_pod_ready(f"{SUBSCRIBERS}-0", hidden=True)
        self.initialize_databases()
        self.apply_documents(
            [
                self.ingest_deployment(),
                self.cronjob(
                    "invoice-run",
                    "30 1 * * *",
                    "invoice-run.sh",
                    "core",
                    "finance",
                    "core-postgres",
                    "finance-jobs",
                    "/opt/finance",
                ),
                self.cronjob(
                    "billing-rerate",
                    "5 * * * *",
                    "rerate.sh",
                    "billing",
                    "billing",
                    "billing-postgres",
                    "billing-jobs",
                    "/opt/billing",
                ),
            ]
        )
        self.wait_rollout("statefulset", "ingest")
        self.apply_documents(self.gateway_deployment(), hidden=True)
        self.wait_rollout("deployment", GATEWAY, hidden=True)

    def initialize_databases(self):
        p = self.passwords
        self.core("create database usage;\ncreate database finance;\ncreate database meter;\n", database="postgres")
        self.core((COMPANY / "meter.sql").read_text(), database="meter")
        self.core((COMPANY / "usage.sql").read_text())
        tenants = ",\n".join(
            f"({sql_literal(t)}, {sql_literal(spec[0])}, {sql_literal(spec[1])}, {str(t in STREAMED).lower()})"
            for t, spec in TENANTS.items()
        )
        roles = [
            f"create role ingest login password {sql_literal(p['ingest'])};",
            "grant select, delete on meter_inbox to ingest;",
            "grant usage on foreign server meter to ingest;",
            f"create user mapping for ingest server meter options (user 'ingest', password {sql_literal(p['ingest'])}, "
            "password_required 'false');",
            "grant select, insert, update on ingest_batches to ingest;",
            "grant select, insert on usage_events to ingest;",
            "grant usage on all sequences in schema public to ingest;",
            f"create role gateway login password {sql_literal(p['gateway'])};",
            f"create role billing_feed login replication password {sql_literal(p['billing_feed'])};",
            "grant select on usage_events to billing_feed;",
        ]
        for db in CUSTOMER_DATABASES:
            roles.append(f"create role feed_{db} login replication password {sql_literal(p['feed_' + db])};")
            roles.append(f"grant select on usage_events to feed_{db};")
        self.core(f"insert into tenants values\n{tenants};\n" + "\n".join(roles) + "\n")
        self.core(
            "grant select, update, delete on meter_inbox to ingest;\n"
            "grant insert on meter_inbox to gateway;\ngrant usage on sequence meter_inbox_id_seq to gateway;\n",
            database="meter",
        )
        self.psql("billing-0", "postgres", "create database billing;\n")
        self.billing(
            (COMPANY / "billing.sql").read_text()
            + (
                f"create role finance_feed login replication password {sql_literal(p['finance_feed'])};\n"
                "grant select on ledger_lines to finance_feed;\n"
            )
        )
        accounts = ",\n".join(
            f"({sql_literal(t)}, {sql_literal(spec[0])}, {spec[3]}, {sql_literal('billing@' + t + '.example')})"
            for t, spec in TENANTS.items()
        )
        self.finance((COMPANY / "finance.sql").read_text() + f"insert into accounts values\n{accounts};\n")
        for db in CUSTOMER_DATABASES:
            receipt = ",\n    export_receipt text,\n    exported_at timestamptz" if db == RECEIPT_DATABASE else ""
            self.psql(f"{SUBSCRIBERS}-0", "postgres", f"create database {db};\n", hidden=True)
            self.customer(
                db,
                f"""create table usage_events (
    event_id bigint primary key,
    record_id text not null,
    tenant_id text not null,
    metric text not null,
    quantity numeric(12,3) not null,
    occurred_at timestamptz not null,
    batch_id bigint not null{receipt}
);
""",
            )

    def subscribe_customers(self, databases=None):
        for db in databases or CUSTOMER_DATABASES:
            tenant = CUSTOMER_DATABASES.get(db) or db.split("_")[0]
            conninfo = (
                f"host=core.{self.namespace}.svc.cluster.local port=5432 dbname=usage "
                f"user=feed_{db} password={self.passwords['feed_' + db]}"
            )
            self.customer(
                db,
                f"create subscription {db} connection {sql_literal(conninfo)} "
                f"publication usage_{tenant}, corr_{tenant};\n",
            )

    def subscribe_billing(self):
        conninfo = f"host=core port=5432 dbname=usage user=billing_feed password={self.passwords['billing_feed']}"
        self.billing(
            f"create subscription billing_usage connection {sql_literal(conninfo)} publication billing_usage;\n"
        )

    def subscribe_finance(self):
        conninfo = f"host=billing port=5432 dbname=billing user=finance_feed password={self.passwords['finance_feed']}"
        self.finance(
            f"create subscription finance_ledger connection {sql_literal(conninfo)} "
            "publication ledger_all, ledger_corr;\n"
        )

    # ------------------------------------------------------------- operations
    def set_rate(self, records_per_second):
        self.hidden(
            "exec", f"deployment/{GATEWAY}", "--", "bash", "-c", f"echo {int(records_per_second)} > /run/load/rate"
        )

    def pubsync(self, target, conf, revision):
        name = f"pubsync-{target}"
        self.apply_documents([self.config_map(name, {"publications.conf": conf})])
        # A mounted ConfigMap reaches a new pod immediately; the Job starts one.
        job = self.pubsync_job(target, revision)
        self.apply_documents([job])
        return self.wait_job(job["metadata"]["name"])

    def wait_job(self, name, timeout=1200):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = json.loads(self.command("get", "job", name, "-o", "json"))["status"]
            if status.get("succeeded"):
                return self.command("logs", f"job/{name}")
            if status.get("failed"):
                raise RuntimeError(f"job {name} failed: {self.command('logs', f'job/{name}')}")
            time.sleep(2)
        raise TimeoutError(f"job {name} did not finish")

    def invoice_run(self, accounts, name):
        # The CronJob's own job, with the accounts to close passed explicitly.
        job = json.loads(self.command("get", "cronjob", "invoice-run", "-o", "json"))["spec"]["jobTemplate"]
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": {"app": "invoice-run"}},
            "spec": job["spec"],
        }
        job["spec"]["template"]["spec"]["containers"][0]["args"] = list(accounts)
        self.apply_documents([job])
        return self.wait_job(name)

    def start_rerate(self, name):
        """Run billing's re-rating job now and return once its update is running."""
        job = json.loads(self.command("get", "cronjob", "billing-rerate", "-o", "json"))["spec"]["jobTemplate"]
        self.apply_documents(
            [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name}, "spec": job["spec"]}]
        )
        deadline = time.monotonic() + 180
        while (
            self.billing(
                "select count(*) from pg_stat_activity where state = 'active' and query like 'with rerated%';"
            ).strip()
            == "0"
        ):
            if time.monotonic() > deadline:
                raise TimeoutError("re-rating did not start")
            time.sleep(0.2)

    def slot_lsns(self, run=None):
        run = run or self.core
        return {
            r["slot"]: r
            for r in self.rows(
                run,
                (
                    "select json_build_object('slot', slot_name, 'active', active, 'confirmed', confirmed_flush_lsn::text, "
                    "'behind', pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) "
                    "from pg_replication_slots where slot_type = 'logical';"
                ),
            )
        }

    def wait_caught_up(self, run=None, slots=None, timeout=600):
        """Wait until the given (or every) logical slot has confirmed the current WAL position."""
        run = run or self.core
        target = run("select pg_current_wal_lsn();").strip()
        deadline = time.monotonic() + timeout
        while True:
            pending = [
                r["slot_name"]
                for r in self.rows(
                    run,
                    (
                        "select json_build_object('slot_name', slot_name) from pg_replication_slots "
                        f"where slot_type = 'logical' and (confirmed_flush_lsn is null or confirmed_flush_lsn < {sql_literal(target)}::pg_lsn)"
                        + (f" and slot_name in ({','.join(sql_literal(s) for s in slots)})" if slots else "")
                        + ";"
                    ),
                )
            ]
            if not pending:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"slots not caught up to {target}: {pending}")
            time.sleep(2)

    def wait_inbox_empty(self, timeout=600):
        deadline = time.monotonic() + timeout
        while self.core("select exists (select 1 from meter_inbox);", database="meter").strip() == "t":
            if time.monotonic() > deadline:
                raise TimeoutError("meter inbox did not drain")
            time.sleep(2)

    def restart_subscription(self, database):
        """The customer's link drops and its worker reconnects."""
        self.customer(database, f"alter subscription {database} disable;\n")
        deadline = time.monotonic() + 120
        while (
            self.core(f"select active from pg_replication_slots where slot_name = {sql_literal(database)};").strip()
            == "t"
        ):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{database} walsender did not stop")
            time.sleep(1)
        time.sleep(3)
        self.customer(database, f"alter subscription {database} enable;\n")
        deadline = time.monotonic() + 120
        while (
            self.core(f"select active from pg_replication_slots where slot_name = {sql_literal(database)};").strip()
            != "t"
        ):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{database} walsender did not start")
            time.sleep(1)

    def set_server(self, name, image=None, senders=None):
        patch = {"spec": {"template": {"spec": {"containers": [{"name": "postgres"}]}}}}
        container = patch["spec"]["template"]["spec"]["containers"][0]
        if image:
            container["image"] = image
        if senders:
            container["args"] = self.server_args(senders)
        self.command("patch", "statefulset", name, "--type=strategic", "-p", json.dumps(patch))
        self.wait_rollout("statefulset", name)
        self.wait_pod_ready(f"{name}-0")

    # ------------------------------------------------------------------ cleanup
    def remove_hidden(self):
        with contextlib.suppress(subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self.hidden(
                "delete",
                f"statefulset/{SUBSCRIBERS}",
                f"deployment/{GATEWAY}",
                "configmap/gateway",
                "configmap/metering-state",
                "secret/subscribers",
                "secret/gateway",
                "networkpolicy/customer-sites",
                f"service/{SUBSCRIBERS}",
                "--ignore-not-found",
                "--wait=true",
                "--timeout=180s",
                timeout=240,
            )
            self.hidden(
                "delete",
                "pvc",
                "-l",
                f"app={SUBSCRIBERS}",
                "--ignore-not-found",
                "--wait=true",
                "--timeout=120s",
                timeout=180,
            )
            self.hidden(
                "delete",
                f"pvc/data-{SUBSCRIBERS}-0",
                "--ignore-not-found",
                "--wait=true",
                "--timeout=120s",
                timeout=180,
            )

    def cleanup(self):
        self.remove_hidden()
        super().cleanup()
