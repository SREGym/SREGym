"""A metered-API company's usage platform on PostgreSQL logical replication.

Core holds usage events. Billing subscribes to all of them and rates every change
into its ledger; finance (a database on core) subscribes to the ledger and
invoices from it; payments (on core) holds card captures and refunds; support
(on core) holds tickets and the correction files sent to customers. Each
enterprise customer's own database subscribes to a row-filtered usage
publication and a corrections publication and books every row it receives on
its usage statement. The customer databases, the API gateways that send meter
records, and the checks live in the hidden namespace.
"""

import base64
import contextlib
import json
import secrets
import select
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
COMMITTED_DISCOUNTS = {"quill": 25, "hollis": 15}

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
STREAMED = sorted(set(CUSTOMER_DATABASES.values()))
#: Support's holding account: usage parked while a correction is re-sent. Never invoiced, never streamed.
HOLDING = ("suspense", "Unallocated usage (support)")
ALLOCATION_CUSTOMERS = {
    db: CUSTOMER_DATABASES[db] for db in ("pell_usage", "quill_usage", "larkspur_live", "ostrava_usage")
}
#: Customer subscriptions created to stop (disable themselves) on the first error.
STOP_ON_ERROR = ("larkspur_monthend",)
SUBSCRIBERS = "subscribers"
GATEWAY = "gateway"


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def daily_schedule(start, offset_minutes):
    """A daily cron schedule at start + offset (UTC): routine jobs never fire while the incident is worked."""
    at = start.astimezone(UTC) + timedelta(minutes=offset_minutes)
    return f"{at.minute} {at.hour} * * *"


def extracts_conf(accounts):
    """The correction-extracts configuration: account -> ticket."""
    lines = ["# account | ticket"] + [f"{account} | {ticket}" for account, ticket in accounts.items()]
    return "\n".join(lines) + "\n"


def parse_extracts_conf(text):
    accounts = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            account, _, ticket = line.partition("|")
            accounts[account.strip()] = ticket.strip()
    return accounts


def publications_conf(revision, comment, operations, *, quill_period=None):
    """The declared publication set; operations maps publication -> publish list."""
    lines = [f"# revision: {revision} {comment}", "# publication | table | row filter | publish"]
    lines.append(f"billing_usage | usage_events | | {operations.get('billing_usage', 'insert,update,delete')}")
    tenants = operations.get("tenants", STREAMED)
    for tenant in tenants:
        usage, corr = operations.get(tenant, ("insert,update,delete", ""))
        lines.append(f"usage_{tenant} | usage_events | tenant_id = '{tenant}' | {usage}")
        lines.append(f"corr_{tenant} | usage_events | tenant_id = '{tenant}' | {corr}")
    if quill_period is not None and "quill" in tenants:
        lines.append(
            "corr_open_quill | usage_events | tenant_id = 'quill' "
            f"AND billing_period = DATE {sql_literal(quill_period)} | update"
        )
    return "\n".join(lines) + "\n"


def ledger_conf(revision, comment, ledger_all, ledger_corr):
    return (
        f"# revision: {revision} {comment}\n# publication | table | row filter | publish\n"
        f"ledger_all | ledger_lines | | {ledger_all}\nledger_corr | ledger_lines | | {ledger_corr}\n"
    )


class MeteringJobFailed(RuntimeError):
    """A configured company job finished unsuccessfully or is missing."""


class ExtractDeliveryTimeout(TimeoutError):
    """The customer file copy still differs from the files sent by support."""


class MeteringApplication(KubernetesCommands, Application):
    def __init__(self):
        self.quill_period = datetime.now(UTC).date().replace(day=1)
        super().__init__(HERE / "metadata.json")
        self.load_app_json()
        self.app_name = self.name
        self.description = self.get_app_json()["Desc"]
        self.kubectl = KubeCtl()
        self.passwords = {}
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
        return run(*self.psql_args(pod, database), input_text=script, timeout=timeout)

    @staticmethod
    def psql_args(pod, database):
        return [
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

    def core(self, script, database="usage", timeout=300):
        return self.psql("core-0", database, script, timeout=timeout)

    def billing(self, script, timeout=300):
        return self.psql("billing-0", "billing", script, timeout=timeout)

    def finance(self, script, timeout=300):
        return self.core(script, database="finance", timeout=timeout)

    def customer(self, database, script, timeout=300):
        return self.psql(f"{SUBSCRIBERS}-0", database, script, hidden=True, timeout=timeout)

    @contextlib.contextmanager
    def customer_session(self, database):
        # A statement can stay open while subsequent batches apply to this reader.
        args = self.psql_args(f"{SUBSCRIBERS}-0", database)
        with subprocess.Popen(
            ["kubectl", "-n", HIDDEN, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as proc:
            def execute(script):
                proc.stdin.write(script + "\n\\echo ready\n")
                proc.stdin.flush()
                if not select.select([proc.stdout], [], [], 30)[0]:
                    raise TimeoutError(f"{database} statement session did not respond")
                if proc.stdout.readline().strip() != "ready":
                    proc.wait(timeout=5)
                    raise RuntimeError(f"{database} statement session: {proc.stderr.read().strip()}")

            try:
                yield execute
            finally:
                proc.stdin.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    def rows(self, run, script):
        """Run a query that returns one JSON document per row."""
        return [json.loads(line) for line in run(script).splitlines() if line.strip()]

    def secret(self, name, key, hidden=False):
        run = self.hidden if hidden else self.command
        data = json.loads(run("get", "secret", name, "-o", "json"))["data"]
        return base64.b64decode(data[key]).decode()

    def apply_documents(self, documents, hidden=False):
        # Server-side apply keeps no last-applied copy of the object on it.
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": documents})
        (self.hidden if hidden else self.command)(
            "apply", "--server-side", "--force-conflicts", "-f", "-", input_text=body, timeout=180
        )

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
            "log_filename=postgresql-%a.log",
            "-c",
            "log_truncate_on_rotation=on",
            "-c",
            "log_rotation_age=1d",
            "-c",
            "log_rotation_size=0",
        ]

    def server_statefulset(self, name, image, *, billing_cpu="200m"):
        if name == "core":
            return self.postgres_statefulset("core", image, self.server_args(), "core-postgres", memory="1536Mi")
        return self.postgres_statefulset("billing", image, self.server_args(), "billing-postgres", cpu=billing_cpu)

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

    def cronjob(self, name, schedule, script, host, database, secret, config_map, mount, extra_mounts=None):
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
                                {config_map: mount, **(extra_mounts or {})},
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
                "template": {"metadata": {"labels": {"app": "pubsync", "target": target}}, "spec": pod},
            },
        }

    def subscribers_statefulset(self):
        return self.postgres_statefulset(
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
        )

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
                                    "env": self.client_env(f"core.{self.namespace}.svc", "meter", "gateway", "gateway")
                                    + [{"name": "GATEWAY_INITIAL_RATE", "value": "0"}],
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
                "feed_files_pell",
                "return_delivery",
                *(f"sign_{db}" for db in ALLOCATION_CUSTOMERS),
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
                self.config_map(
                    "billing-jobs",
                    {name: (COMPANY / name).read_text() for name in ("rerate.sh", "rerate.sql", "r5-rate-bands.sql")},
                ),

                self.config_map(
                    "finance-jobs",
                    {
                        name: (COMPANY / name).read_text()
                        for name in ("invoice-run.sh", "statement-return-import.sh", "statement-returns.sql")
                    },
                ),
                self.config_map(
                    "support-jobs", {"correction-extracts.sh": (COMPANY / "correction-extracts.sh").read_text()}
                ),
                self.server_statefulset("core", OLD_IMAGE),
                self.service("core"),
                # Replay the bulk history at full speed. The patch rollout restores the operating limit.
                self.server_statefulset("billing", OLD_IMAGE, billing_cpu="1"),
                self.service("billing"),
            ]
        )
        self.apply_documents(
            [
                self.secret_document("subscribers", {"password": p["subscribers"]}),
                self.secret_document("gateway", {"password": p["gateway"]}),
                self.hidden_policy(),
                self.subscribers_statefulset(),
                self.service(SUBSCRIBERS),
                self.config_map(
                    "statement-returns",
                    {"statement-return-resend.sh": (COMPANY / "statement-return-resend.sh").read_text()},
                ),
            ],
            hidden=True,
        )
        with ThreadPoolExecutor(max_workers=3) as pool:
            billing = pool.submit(self.initialize_billing)
            customers = pool.submit(self.initialize_customers)
            self.wait_pod_ready("core-0")
            self.initialize_databases()
            billing.result()
            customers.result()
        start = datetime.now(UTC)
        self.apply_documents(
            [
                self.ingest_deployment(),
                self.cronjob(
                    "invoice-run",
                    daily_schedule(start, 12 * 60 + 20),
                    "invoice-run.sh",
                    "core",
                    "finance",
                    "core-postgres",
                    "finance-jobs",
                    "/opt/finance",
                ),
                self.cronjob(
                    "statement-return-import",
                    daily_schedule(start, 12 * 60 + 15),
                    "statement-return-import.sh",
                    "core",
                    "finance",
                    "core-postgres",
                    "finance-jobs",
                    "/opt/finance",
                ),
                self.cronjob(
                    "billing-rerate",
                    daily_schedule(start, 12 * 60 + 5),
                    "rerate.sh",
                    "billing",
                    "billing",
                    "billing-postgres",
                    "billing-jobs",
                    "/opt/billing",
                ),
            ]
        )
        self.apply_documents(
            [
                *self.gateway_deployment(),
                self.cronjob(
                    "statement-return-resend",
                    daily_schedule(start, 12 * 60 + 10),
                    "statement-return-resend.sh",
                    SUBSCRIBERS,
                    "postgres",
                    "subscribers",
                    "statement-returns",
                    "/opt/returns",
                ),
            ],
            hidden=True,
        )
        self.wait_rollout("statefulset", "ingest")
        self.wait_rollout("deployment", GATEWAY, hidden=True)

    def initialize_databases(self):
        p = self.passwords
        self.core(
            "create database usage;\ncreate database finance;\ncreate database meter;\n"
            "create database payments;\ncreate database support;\n",
            database="postgres",
        )
        self.core((COMPANY / "payments.sql").read_text(), database="payments")
        self.core((COMPANY / "support.sql").read_text(), database="support")
        keys = ", ".join(
            f"({sql_literal(account)}, {sql_literal(p[f'sign_{db}'])})"
            for db, account in ALLOCATION_CUSTOMERS.items()
        )
        self.core(
            f"create role return_delivery login password {sql_literal(p['return_delivery'])};"
            "grant execute on function receive_statement_returns(text, jsonb, jsonb) to return_delivery;"
            f"insert into return_signing_keys values {keys};",
            database="support",
        )
        self.core((COMPANY / "meter.sql").read_text(), database="meter")
        self.core((COMPANY / "usage.sql").read_text())
        tenants = ",\n".join(
            f"({sql_literal(t)}, {sql_literal(spec[0])}, {sql_literal(spec[1])}, {str(t in STREAMED).lower()})"
            for t, spec in TENANTS.items()
        ) + f",\n({sql_literal(HOLDING[0])}, {sql_literal(HOLDING[1])}, 'internal', false)"
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
        accounts = ",\n".join(
            f"({sql_literal(t)}, {sql_literal(spec[0])}, {spec[3]}, {sql_literal('billing@' + t + '.example')}, "
            f"{COMMITTED_DISCOUNTS.get(t, 0)})"
            for t, spec in TENANTS.items()
        ) + f",\n({sql_literal(HOLDING[0])}, {sql_literal(HOLDING[1])}, 1, 'support@metering.example', 0)"
        self.finance(
            (COMPANY / "finance.sql").read_text()
            + (COMPANY / "statement-returns.sql").read_text()
            + f"insert into accounts values\n{accounts};\n"
        )

    def initialize_billing(self):
        self.wait_pod_ready("billing-0")
        p = self.passwords
        self.psql("billing-0", "postgres", "create database billing;\n")
        self.billing(
            (COMPANY / "billing.sql").read_text()
            + (
                f"create role finance_feed login replication password {sql_literal(p['finance_feed'])};\n"
                "grant select on ledger_lines to finance_feed;\n"
            )
        )

    def initialize_customers(self):
        self.wait_pod_ready(f"{SUBSCRIBERS}-0", hidden=True)
        for db in CUSTOMER_DATABASES:
            self.create_customer_database(db)
            if db in ALLOCATION_CUSTOMERS:
                account = sql_literal(ALLOCATION_CUSTOMERS[db])
                key = sql_literal(self.passwords[f"sign_{db}"])
                host = sql_literal(f"core.{self.namespace}.svc.cluster.local")
                password = sql_literal(self.passwords["return_delivery"])
                self.customer(
                    db,
                    f"insert into return_sender values ({account}, {key});"
                    "create server statement_support foreign data wrapper dblink_fdw "
                    f"options (host {host}, dbname 'support');"
                    "create user mapping for postgres server statement_support "
                    f"options (user 'return_delivery', password {password});",
                )

    def publish_allocation_receipts(self):
        self.core("\n".join(
            f"alter publication usage_{account} add table allocation_receipts where (tenant_id = '{account}');"
            f"grant select on allocation_receipts to feed_{db};"
            + ("grant select on allocation_receipts to feed_larkspur_monthend;" if account == "larkspur" else "")
            for db, account in ALLOCATION_CUSTOMERS.items()
        ))

    def create_customer_database(self, db):
        self.psql(f"{SUBSCRIBERS}-0", "postgres", f"create database {db};\n", hidden=True)
        quill = db == "quill_usage"
        columns = "event_id, billing_period" if quill else "event_id"
        partition = "partition by list (billing_period)" if quill else ""
        self.customer(
            db,
            f"\\set partition_by_period {str(quill).lower()}\n"
            f"\\set open_period {self.quill_period.isoformat()}\n"
            f"\\set identity_columns '{columns}'\n"
            f"\\set partition_clause '{partition}'\n"
            + (COMPANY / "customer.sql").read_text(),
        )

    def subscribe_customers(self, databases=None, *, copy_data=True):
        for db in databases or CUSTOMER_DATABASES:
            tenant = CUSTOMER_DATABASES.get(db) or db.split("_")[0]
            conninfo = (
                f"host=core.{self.namespace}.svc.cluster.local port=5432 dbname=usage "
                f"user=feed_{db} password={self.passwords['feed_' + db]}"
            )
            # Larkspur's month-end copy must never take a partial close: it stops on the first error.
            options = f"copy_data = {str(copy_data).lower()}"
            if db in STOP_ON_ERROR:
                options += ", disable_on_error = true"
            publications = f"usage_{tenant}, corr_{tenant}"
            if db == "quill_usage":
                publications += ", corr_open_quill"
            self.customer(
                db,
                f"create subscription {db} connection {sql_literal(conninfo)} "
                f"publication {publications} with ({options});\n",
            )

    def subscribe_files(self, account="pell", database="pell_usage"):
        """Correction files reach the customer by logical replication: new files only, filtered to its account."""
        name = f"files_{account}"
        password = self.passwords[f"feed_{name}"]
        self.core(
            f"create role feed_{name} login replication password {sql_literal(password)};\n"
            f"grant select on extract_files to feed_{name};\n"
            f"create publication {name} for table extract_files where (account = {sql_literal(account)}) "
            "with (publish = 'insert');\n",
            database="support",
        )
        conninfo = (
            f"host=core.{self.namespace}.svc.cluster.local port=5432 dbname=support "
            f"user=feed_{name} password={password}"
        )
        self.customer(database, f"create subscription {name} connection {sql_literal(conninfo)} publication {name};\n")

    def subscription_origin(self, database, subscription=None):
        """Where the customer's subscription has applied up to (its replication origin), as a pg_lsn text."""
        return self.customer(
            database,
            "select coalesce(o.remote_lsn, '0/0') from pg_subscription s left join pg_replication_origin_status o "
            f"on o.external_id = 'pg_' || s.oid where s.subname = {sql_literal(subscription or database)};",
        ).strip()

    def subscribe_billing(self, *, copy_data=True):
        conninfo = f"host=core port=5432 dbname=usage user=billing_feed password={self.passwords['billing_feed']}"
        self.billing(
            f"create subscription billing_usage connection {sql_literal(conninfo)} publication billing_usage "
            f"with (copy_data = {str(copy_data).lower()});\n"
        )

    def subscribe_finance(self, *, copy_data=True):
        conninfo = f"host=billing port=5432 dbname=billing user=finance_feed password={self.passwords['finance_feed']}"
        self.finance(
            f"create subscription finance_ledger connection {sql_literal(conninfo)} "
            f"publication ledger_all, ledger_corr with (copy_data = {str(copy_data).lower()});\n"
        )

    # ------------------------------------------------------------- operations
    def set_rate(self, records_per_second):
        rate = int(records_per_second)
        request = str(rate)
        if rate == 0:
            request = f"0 {secrets.token_hex(8)}"
        script = f"echo '{request}' > /run/load/next-rate && mv /run/load/next-rate /run/load/rate"
        if rate == 0:
            # Wait for the gateway loop to finish its last flush before draining the inbox.
            script += (
                f' && until [ "$(cat /run/load/observed-rate 2>/dev/null)" = \'{request}\' ]; '
                "do sleep 0.1; done"
            )
        self.hidden("exec", f"deployment/{GATEWAY}", "--", "timeout", "30", "bash", "-c", script)

    def pubsync(self, target, conf, revision):
        name = f"pubsync-{target}"
        job = self.pubsync_job(target, revision)
        self.apply_documents([self.config_map(name, {"publications.conf": conf}), job])
        return self.wait_job(job["metadata"]["name"])

    def replay_pubsync(self, target, conf):
        self.apply_documents([self.config_map(f"pubsync-{target}", {"publications.conf": conf})])
        database = "billing" if target == "billing" else "usage"
        return self.replay_job("pubsync.sh", database, pod=f"{target}-0", config=conf)

    def wait_job(self, name, timeout=1200, *, customer_side=False):
        command = self.hidden if customer_side else self.command
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = json.loads(command("get", "job", name, "-o", "json"))["status"]
            if status.get("succeeded"):
                return command("logs", f"job/{name}")
            if any(c["type"] == "Failed" and c["status"] == "True" for c in status.get("conditions", ())):
                pods = json.loads(command("get", "pods", "-l", f"job-name={name}", "-o", "json"))["items"]
                states = [
                    container.get("state", {}).get("terminated", {})
                    for pod in pods
                    for container in pod.get("status", {}).get("containerStatuses", ())
                ]
                logs = command("logs", f"job/{name}")
                error = f"job {name} failed: {logs}"
                if any(state.get("reason") == "Error" and state.get("exitCode") for state in states):
                    raise MeteringJobFailed(error)
                raise RuntimeError(error)
            time.sleep(0.5)
        raise TimeoutError(f"job {name} did not finish")

    def run_cronjob(self, cronjob, name, args=None, *, customer_side=False):
        """Run a CronJob's own job now (as `kubectl create job --from` does) and wait for it."""
        command = self.hidden if customer_side else self.command
        data = command("get", "cronjob", cronjob, "--ignore-not-found", "-o", "json")
        if not data.strip():
            raise MeteringJobFailed(f"configured job {cronjob} is missing")
        spec = json.loads(data)["spec"]
        if spec.get("suspend"):
            raise MeteringJobFailed(f"configured job {cronjob} is suspended")
        template = spec["jobTemplate"]
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": {"app": cronjob}},
            "spec": template["spec"],
        }
        if args is not None:
            job["spec"]["template"]["spec"]["containers"][0]["args"] = list(args)
        self.apply_documents([job], hidden=customer_side)
        return self.wait_job(name, customer_side=customer_side)

    def invoice_run(self, accounts, name):
        # The CronJob's own job, with the accounts to close passed explicitly.
        return self.run_cronjob("invoice-run", name, accounts)

    def replay_job(self, script, database, *args, pod="core-0", config=None):
        """Run a company job's script in the database pod while replaying its past runs."""
        body = (COMPANY / script).read_text()
        if config is not None:
            args = (body,)
            body = """
set -eu
config_file=$(mktemp)
trap 'rm -f "$config_file"' EXIT
cat > "$config_file"
bash -c "$1" "$0" "$config_file"
"""
        return self.command(
            "exec",
            "-i",
            pod,
            "-c",
            "postgres",
            "--",
            "env",
            "PGUSER=postgres",
            f"PGDATABASE={database}",
            "bash",
            "-c",
            body,
            script,
            *args,
            input_text=config,
        )

    def capture_invoices(self):
        """The card processor's settlement import: capture every sent invoice not yet captured."""
        captured = set(self.core("select invoice_id from captures;", database="payments").split())
        values = [
            f"({sql_literal(r['account'])}, {r['invoice_id']}, {r['total']})"
            for r in self.rows(
                self.finance,
                "select json_build_object('account', account, 'invoice_id', invoice_id, 'total', total) "
                "from invoices where status = 'sent' and total > 0 order by invoice_id;",
            )
            if str(r["invoice_id"]) not in captured
        ]
        if values:
            self.core(
                "insert into captures (account, invoice_id, amount) values " + ", ".join(values) + ";",
                database="payments",
            )
        return len(values)

    def setup_extracts(self, accounts):
        """Support's nightly correction files for the listed accounts."""
        invoice_schedule = json.loads(self.command("get", "cronjob", "invoice-run", "-o", "json"))["spec"]["schedule"]
        minute, hour = (int(x) for x in invoice_schedule.split()[:2])
        at = datetime(2000, 1, 1, hour, minute, tzinfo=UTC)
        self.apply_documents(
            [
                self.config_map("correction-extracts", {"extracts.conf": extracts_conf(accounts)}),
                self.cronjob(
                    "correction-extracts",
                    daily_schedule(at, -15),
                    "correction-extracts.sh",
                    "core",
                    "usage",
                    "core-postgres",
                    "support-jobs",
                    "/opt/support",
                    extra_mounts={"correction-extracts": "/etc/extracts"},
                ),
            ]
        )

    def close_statement(self, database):
        """The customer's statement close (customer code)."""
        return int(self.customer(database, "select close_statement();").strip())

    def deliver_statement_return(self, database, return_ref):
        """Send a customer's saved signed return through the normal delivery function."""
        return int(self.customer(database, f"select send_statement_returns({sql_literal(return_ref)});").strip())

    def wait_extract_files(self, account, database, timeout=180):
        """Wait for every file and its current delivery status."""
        columns = "select file_id || '|' || status from extract_files"
        sent = set(self.core(f"{columns} where account = {sql_literal(account)};", database="support").splitlines())
        deadline = time.monotonic() + timeout
        while missing := sent - set(self.customer(database, columns + ";").splitlines()):
            if time.monotonic() > deadline:
                raise ExtractDeliveryTimeout(f"{database} did not receive file states {sorted(missing)}")
            time.sleep(2)

    def load_extract_files(self, account, database):
        """Apply ready files after the file copy has received any withdrawals."""
        self.wait_extract_files(account, database)
        return int(self.customer(database, "select load_ready_files();").strip())

    def truncate_server_logs(self):
        """Construction cleanup: the servers' log files start with the story."""
        for pod in ("core-0", "billing-0"):
            self.command(
                "exec",
                pod,
                "-c",
                "postgres",
                "--",
                "bash",
                "-c",
                "find /var/lib/postgresql/data/pgdata/log -type f -exec truncate -s 0 {} +",
            )

    @staticmethod
    def usage_batch_sql(batches, records, *, starting_at=None):
        """Seed normal usage in complete batches and let the existing feeds build their copies."""
        tenants = [t for t, spec in TENANTS.items() for _ in range(spec[2])]
        start = starting_at or "clock_timestamp() - interval '20 seconds'"
        return f"""
with batches as (
    insert into ingest_batches (source, records)
    select 'ingest-' || ((i - 1) % 4 / 2) || '/' || ((i - 1) % 4), {records}
    from generate_series(1, {batches}) i
    returning batch_id, records
)
insert into usage_events (record_id, tenant_id, metric, quantity, occurred_at, batch_id)
select gen_random_uuid()::text, tenant, metric,
       case metric when 'api_calls' then 1 + floor(random() * 5000)
                   when 'tokens_k' then round((1 + random() * 800)::numeric, 3)
                   else round((0.5 + random() * 40)::numeric, 3) end,
       ({start}) + random() * interval '20 seconds', batch_id
from (
    select (string_to_array('{','.join(tenants)}', ','))[1 + floor(random() * {len(tenants)})::int] as tenant,
           (array['api_calls', 'tokens_k', 'storage_gb_h'])[1 + floor(random() * 3)::int] as metric,
           batch_id
    from batches cross join lateral generate_series(1, records)
) records;
"""

    def publication_handoff(self, target, conf):
        """Commit the make while a transaction that wrote on this publisher is still open."""
        revision = conf.splitlines()[0].removeprefix("# revision: ")
        run, write = (
            (self.billing, (COMPANY / "rerate.sql").read_text())
            if target == "billing"
            else (self.core, self.usage_batch_sql(1, 60))
        )
        return self.change_while_writing(
            run, "metering handoff", write,
            f"select 1 from ops.pubsync_log where revision = {sql_literal(revision)}",
            lambda: self.replay_pubsync(target, conf),
        )

    def release_rate_bands(self):
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "billing-r5-rate-bands"},
            "spec": {
                "backoffLimit": 0,
                "template": {
                    "spec": self.job_pod(
                        "release",
                        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", "/opt/billing/r5-rate-bands.sql"],
                        self.client_env("billing", "billing", "postgres", "billing-postgres"),
                        {"billing-jobs": "/opt/billing"},
                    )
                },
            },
        }

        def release():
            self.apply_documents([job])
            self.wait_job(job["metadata"]["name"])

        # The hourly job is applying the newest batch. Older invoices keep
        # their existing rate bands, including usage booked before r5.
        batch = """
from price_book p where l.metric = p.metric
and l.line_id > (select max(line_id) - 60 from ledger_lines)
"""
        return self.change_while_writing(
            self.billing,
            "billing hourly re-rate",
            f"update ledger_lines l set amount = round(l.quantity * p.unit_price, 4), "
            f"rate.unit_price = p.unit_price {batch};",
            "select 1 from ops.billing_releases where revision = 'r5 rate bands'",
            release,
            f"update ledger_lines l set rate = rate_snapshot(p), rated_at = statement_timestamp() {batch};",
        )

    def change_while_writing(self, run, writer_name, write, released, change, finish=""):
        # The release record and catalog change commit together. The writer
        # cannot commit until then, and the release waits for its first write.
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(
                run,
                f"""
begin;
set local application_name = {sql_literal(writer_name)};
{write}
do $$
declare deadline timestamptz := clock_timestamp() + interval '90 seconds';
begin
    while not exists ({released}) loop
        if clock_timestamp() > deadline then
            raise exception 'catalog change did not commit';
        end if;
        perform pg_sleep(0.05);
    end loop;
end $$;
{finish}
commit;
select pg_current_wal_lsn();
""",
            )
            # This statement starts only after the writer's INSERT or UPDATE has finished.
            deadline = time.monotonic() + 90
            while True:
                if writer.done():
                    writer.result()
                    raise RuntimeError(f"{writer_name} committed before the catalog change")
                if run(
                    "select exists (select 1 from pg_stat_activity "
                    f"where application_name = {sql_literal(writer_name)} "
                    "and backend_xid is not null and query like 'do $$%');"
                ).strip() == "t":
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{writer_name} did not finish its first write")
                time.sleep(0.2)
            change()
            return writer.result().splitlines()[-1]

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

    def wait_caught_up(self, run=None, slots=None, timeout=600, skip=(), target=None):
        """Wait until the given (or every) logical slot confirms the target WAL position."""
        run = run or self.core
        target = target or run("select pg_current_wal_lsn();").strip()
        lag = f"(s.confirmed_flush_lsn is null or s.confirmed_flush_lsn < {sql_literal(target)}::pg_lsn)"
        if slots:
            names = ", ".join(sql_literal(slot) for slot in slots if slot not in skip)
            if not names:
                return
            query = (
                "select json_build_object('slot_name', wanted.name) "
                f"from unnest(array[{names}]) wanted(name) left join pg_replication_slots s "
                f"on s.slot_name = wanted.name and s.slot_type = 'logical' where {lag};"
            )
        else:
            omitted = f" and s.slot_name not in ({','.join(sql_literal(s) for s in skip)})" if skip else ""
            query = (
                "select json_build_object('slot_name', s.slot_name) from pg_replication_slots s "
                f"where s.slot_type = 'logical' and {lag}{omitted};"
            )
        deadline = time.monotonic() + timeout
        while True:
            pending = [r["slot_name"] for r in self.rows(run, query)]
            if not pending:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"slots not caught up to {target}: {pending}")
            time.sleep(0.5)

    def wait_inbox_empty(self, timeout=600):
        deadline = time.monotonic() + timeout
        while (
            self.core("select exists (select 1 from meter_inbox);", database="meter").strip() == "t"
            or self.core(
                "select exists (select 1 from pg_stat_activity where usename = 'ingest' and backend_xid is not null);"
            ).strip() == "t"
        ):
            if time.monotonic() > deadline:
                raise TimeoutError("meter inbox did not drain")
            time.sleep(0.5)

    def slot_active(self, slot, run=None):
        return (run or self.core)(
            f"select active from pg_replication_slots where slot_name = {sql_literal(slot)};"
        ).strip() == "t"

    def wait_slot(self, slot, active, run=None, timeout=120):
        deadline = time.monotonic() + timeout
        while self.slot_active(slot, run) != active:
            if time.monotonic() > deadline:
                raise TimeoutError(f"slot {slot} did not become {'active' if active else 'inactive'}")
            time.sleep(1)

    def restart_subscription(self, database, subscriber=None, publisher=None):
        """The subscription's link drops and its worker reconnects (customer side by default)."""
        subscriber = subscriber or (lambda q: self.customer(database, q))
        subscriber(f"alter subscription {database} disable;\n")
        self.wait_slot(database, False, publisher)
        subscriber(f"alter subscription {database} enable;\n")
        self.wait_slot(database, True, publisher)

    def bounce_finance(self):
        """Finance's DBA maintenance: its subscription to billing is disabled and enabled again."""
        self.restart_subscription("finance_ledger", subscriber=self.finance, publisher=self.billing)

    def pause_reader(self, database, *, applied_ahead=False):
        """Refuse new connections and end the walsender, optionally before it confirms an applied batch."""
        slot = sql_literal(database)
        deadline = time.monotonic() + 60
        while True:
            self.core(f"alter role feed_{database} nologin;")
            while True:
                condition = ""
                if applied_ahead:
                    origin = self.subscription_origin(database)
                    condition = f"and confirmed_flush_lsn < {sql_literal(origin)}::pg_lsn"
                terminated = self.core(
                    "select pg_terminate_backend(active_pid) from pg_replication_slots "
                    f"where slot_name = {slot} and active_pid is not null {condition};"
                ).strip()
                if not applied_ahead or terminated == "t":
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{database} did not apply a batch ahead of its slot")
                time.sleep(0.1)
            self.wait_slot(database, False)
            if not applied_ahead:
                return
            origin = self.subscription_origin(database)
            if self.core(
                f"select confirmed_flush_lsn < {sql_literal(origin)}::pg_lsn "
                f"from pg_replication_slots where slot_name = {slot};"
            ).strip() == "t":
                return
            # An acknowledgement can reach the sender between the query and shutdown.
            # Reconnect only after the original apply worker has handled the disconnect.
            while self.customer(
                database,
                f"select exists (select 1 from pg_stat_subscription where subname = {slot} and pid is not null);",
            ).strip() == "t":
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{database} did not stop after confirming the pause position")
                time.sleep(0.1)
            if time.monotonic() > deadline:
                raise TimeoutError(f"{database} kept confirming its applied batch before the pause")
            self.core(f"alter role feed_{database} login;")
            self.customer(database, f"alter subscription {database} enable;")
            self.wait_slot(database, True)

    def set_server(self, name, image):
        self.apply_documents([self.server_statefulset(name, image)])
        self.wait_rollout("statefulset", name)
        self.wait_pod_ready(f"{name}-0")

    def patch_publishers(self):
        """Pause the reciprocal feeds while their publisher services roll."""
        self.billing("alter subscription billing_usage disable;")
        self.finance("alter subscription finance_ledger disable;")
        with ThreadPoolExecutor(max_workers=2) as pool:
            billing = pool.submit(self.set_server, "billing", NEW_IMAGE)
            self.set_server("core", NEW_IMAGE)
            billing.result()
        for pod, service in (("billing-0", "core"), ("core-0", "billing")):
            deadline = time.monotonic() + 30
            while True:
                try:
                    self.command(
                        "exec", pod, "-c", "postgres", "--", "pg_isready", "-q", "-t", "2", "-U", "postgres", "-h", service,
                        timeout=10,
                    )
                    break
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{service} did not accept connections from {pod} after patching") from exc
                    time.sleep(0.5)
        self.billing("alter subscription billing_usage enable;")
        self.finance("alter subscription finance_ledger enable;")

    # ------------------------------------------------------------------ cleanup
    def remove_hidden(self):
        with contextlib.suppress(subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self.hidden(
                "delete",
                f"statefulset/{SUBSCRIBERS}",
                f"deployment/{GATEWAY}",
                "configmap/gateway",
                "configmap/metering-state",
                "configmap/statement-returns",
                "cronjob/statement-return-resend",
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
