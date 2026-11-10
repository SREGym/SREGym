"""n8n 2.43.5 in queue mode, deployed with the official n8n-hosting Helm chart, with a small payables business.

n8n-io/n8n @ d811f5e8 with the n8n-hosting chart @ 5688ee86: one main, two
workers at concurrency 10 with n8nio/runners task-runner sidecars, PostgreSQL 18
and Redis 7.4 on PVCs, ``Recreate`` strategy, NodePort main Service. One
setting is added: ``DB_PING_MAX_FAILURES_BEFORE_RECOVERY=30`` (default 3, i.e.
about 6 s of failed pings). On a shared, heavily loaded host, I/O stalls of a
few seconds otherwise trigger n8n's database pool recovery, which can leave a
worker with an ended pool that fails every job (``Cannot use a pool after
calling end on the pool``), an unrelated and loud failure. n8n's own config
documentation recommends raising the value for exactly that case. The chart is
vendored (``helm package`` of charts/n8n at 5688ee86) and checked against its
SHA-256. Images are the upstream ones, pinned by digest:
``n8nio/n8n:2.43.5-d811f5e`` and ``n8nio/runners:2.43.5`` (linux/amd64), plus
pinned PostgreSQL and Redis digests.

The business around it: ``payables-portal`` (approved invoices and their
payment status, with each object's history) and ``payments-gateway`` (the bank
API: payouts settle 24 h after they are scheduled and can be cancelled until
then; it honours an ``Idempotency-Key`` header) are small standard-library
Python services in a stock python image. A subclass names the published
workflow, created through n8n's REST API as the owner, and the workload, which
runs in the conductor process and reaches the NodePorts from outside the
cluster.
"""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.apps.n8n.traffic import http
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
PYTHON_IMAGE = "python:3.12.13-alpine3.23"
POSTGRES_IMAGE = "postgres@sha256:9770878138a2e83abcb10856d30cc3f24dae0d317bb956db6ab9159de6c3c80d"
REDIS_IMAGE = "redis@sha256:026c880268ce52fb3a63b66150af583108a38c13af7633e325ae82a145f53a87"
BROWSER_ID = "6b1f0c9e-3d2a-4f57-9e1b-7c8d2a4f5e60"


class N8nRest:
    """Owner session against n8n's REST API (what the editor UI uses)."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, timeout=60):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "browser-id": BROWSER_ID, "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        payload = json.loads(raw) if raw else None
        if status >= 300:
            raise RuntimeError(f"n8n {method} {path} answered {status}: {str(payload)[:500]}")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def login(self, email, password):
        return self.call("POST", "/rest/login", {"emailOrLdapLoginId": email, "password": password})


class N8nPayables(KubernetesCommands, Application):
    """Deployment, REST session and inspection helpers; subclasses set the workflow and the workload."""

    app_id = "n8n"
    metadata_file: Path
    CHART = HERE / "n8n-1.16.0.tgz"
    CHART_SHA256 = "3668937c270ebdade82f06e65d352b8d63177b184038129b6d272743ed306f0d"
    VALUES = HERE / "values.yaml"
    RELEASE = "n8n"
    WORKFLOW_NAME: str
    #: Workflow definitions created and published at setup.
    WORKFLOW_FILES: tuple[Path, ...] = ()
    #: Production webhook whose registration shows the published workflow is live.
    WEBHOOK_PATH: str
    #: Workload class, constructed with the ``endpoints`` callable.
    TRAFFIC: type
    #: payments-gateway environment.
    GATEWAY_ENV = {"GATEWAY_DB": "/data/payouts.db", "SETTLEMENT_HOURS": "24"}
    READY_TIMEOUT_S = 1200
    DEPLOYMENTS = ("postgres", "redis", "payables-portal", "payments-gateway", "n8n-main", "n8n-worker")

    def __init__(self):
        super().__init__(self.metadata_file)
        self.load_app_json()
        self.app_name = self.name
        self.description = self.get_app_json()["Desc"]
        self.kubectl = KubeCtl()
        self.frontend_service = "n8n-main"
        self.frontend_port = 5678
        self.helm_deploy = True
        self.traffic = None
        self.workflow_id: str | None = None
        self.workflow_ids: dict[str, str] = {}
        self._endpoints: dict[str, str] | None = None

    # ------------------------------------------------------------------ manifests
    @staticmethod
    def pvc(name, size):
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}},
        }

    @staticmethod
    def service(name, port, node_port=False):
        spec = {"selector": {"app": name}, "ports": [{"name": "http", "port": port, "targetPort": port}]}
        if node_port:
            spec["type"] = "NodePort"
        return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name}, "spec": spec}

    @staticmethod
    def deployment(name, container, volumes):
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "labels": {"app": name}},
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "containers": [{"name": name, "imagePullPolicy": "IfNotPresent", **container}],
                        "volumes": volumes,
                    },
                },
            },
        }

    def backing_documents(self):
        postgres = self.deployment(
            "postgres",
            {
                "image": POSTGRES_IMAGE,
                "env": [
                    {"name": "POSTGRES_USER", "value": "n8n"},
                    {"name": "POSTGRES_DB", "value": "n8n"},
                    {
                        "name": "POSTGRES_PASSWORD",
                        "valueFrom": {"secretKeyRef": {"name": "n8n-db-secret", "key": "password"}},
                    },
                ],
                "ports": [{"containerPort": 5432}],
                "readinessProbe": {"exec": {"command": ["pg_isready", "-U", "n8n", "-d", "n8n"]}, "periodSeconds": 5},
                "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql"}],
                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
            },
            [{"name": "data", "persistentVolumeClaim": {"claimName": "postgres-data"}}],
        )
        redis = self.deployment(
            "redis",
            {
                "image": REDIS_IMAGE,
                "ports": [{"containerPort": 6379}],
                # The probe timings of the widely used Bitnami Redis chart (first check after 20 s).
                "readinessProbe": {
                    "exec": {"command": ["redis-cli", "ping"]},
                    "initialDelaySeconds": 20,
                    "periodSeconds": 5,
                },
                "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "512Mi"}},
            },
            [{"name": "data", "persistentVolumeClaim": {"claimName": "redis-data"}}],
        )
        postgres_service = self.service("postgres", 5432)
        postgres_service["spec"]["ports"][0]["name"] = "postgres"
        redis_service = self.service("redis", 6379)
        redis_service["spec"]["ports"][0]["name"] = "redis"
        return [
            self.pvc("postgres-data", "5Gi"),
            self.pvc("redis-data", "1Gi"),
            postgres,
            redis,
            postgres_service,
            redis_service,
        ]

    def python_service(self, name, script, env):
        config = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name},
            "data": {script: (RUNTIME / script).read_text()},
        }
        deployment = self.deployment(
            name,
            {
                "image": PYTHON_IMAGE,
                "command": ["python", "-u", f"/opt/{name}/{script}"],
                "env": [{"name": k, "value": v} for k, v in env.items()],
                "ports": [{"containerPort": 8080}],
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 5},
                "volumeMounts": [
                    {"name": "app", "mountPath": f"/opt/{name}", "readOnly": True},
                    {"name": "data", "mountPath": "/data"},
                ],
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            [
                {"name": "app", "configMap": {"name": name}},
                {"name": "data", "persistentVolumeClaim": {"claimName": f"{name}-data"}},
            ],
        )
        return [config, self.pvc(f"{name}-data", "1Gi"), deployment, self.service(name, 8080, node_port=True)]

    def business_documents(self):
        return [
            *self.python_service("payables-portal", "payables_portal.py", {"PORTAL_DB": "/data/portal.db"}),
            *self.python_service("payments-gateway", "payments_gateway.py", dict(self.GATEWAY_ENV)),
        ]

    def ensure_secrets(self):
        existing = set(self.command("get", "secrets", "-o", "name").split())
        token = secrets.token_urlsafe
        wanted = {
            "n8n-db-secret": {"password": "aA1" + token(36)},
            "n8n-core-secrets": {
                "N8N_ENCRYPTION_KEY": "aA1" + token(45),
                "N8N_RUNNERS_AUTH_TOKEN": token(36),
                "N8N_HOST": f"n8n-main.{self.namespace}.svc.cluster.local",
                "N8N_PORT": "5678",
                "N8N_PROTOCOL": "http",
            },
            "n8n-owner": {"email": "payables-ops@northwind-trading.example", "password": "Pay" + token(18) + "9"},
        }
        for name, data in wanted.items():
            if f"secret/{name}" in existing:
                continue
            self.command("create", "secret", "generic", name, *[f"--from-literal={k}={v}" for k, v in data.items()])

    def secret(self, name, key):
        raw = self.command("get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
        return base64.b64decode(raw).decode()

    # ------------------------------------------------------------------ deployment
    def deploy(self):
        digest = hashlib.sha256(self.CHART.read_bytes()).hexdigest()
        if digest != self.CHART_SHA256:
            raise RuntimeError(f"Vendored chart {self.CHART.name} has digest {digest}, expected {self.CHART_SHA256}")
        self.kubectl.create_namespace_if_not_exist(self.namespace)
        self.ensure_secrets()
        self.apply(self.backing_documents())
        self.apply(self.business_documents())
        for name in ("postgres", "redis"):
            self.rollout(name)
        if not Helm.exists_release(self.RELEASE, self.namespace):
            Helm.install(
                release_name=self.RELEASE,
                chart_path=str(self.CHART),
                namespace=self.namespace,
                remote_chart=True,
                extra_args=["-f", str(self.VALUES)],
            )
        for name in self.DEPLOYMENTS:
            self.rollout(name)
        self.setup_n8n()

    def rollout(self, name, timeout=None):
        timeout = timeout or self.READY_TIMEOUT_S
        self.command("rollout", "status", f"deployment/{name}", f"--timeout={timeout}s", timeout=timeout + 30)

    def endpoints(self, refresh: bool = False) -> dict[str, str]:
        if refresh or self._endpoints is None:
            nodes = json.loads(
                subprocess.run(
                    ["kubectl", "get", "nodes", "-o", "json"], capture_output=True, text=True, check=True, timeout=60
                ).stdout
            )["items"]
            worker = next(
                (n for n in nodes if "node-role.kubernetes.io/control-plane" not in n["metadata"]["labels"]), nodes[0]
            )
            ip = next(a["address"] for a in worker["status"]["addresses"] if a["type"] == "InternalIP")
            ports = {}
            for key, service in (("n8n", "n8n-main"), ("portal", "payables-portal"), ("gateway", "payments-gateway")):
                ports[key] = self.command("get", "service", service, "-o", "jsonpath={.spec.ports[0].nodePort}").strip()
            self._endpoints = {key: f"http://{ip}:{port}" for key, port in ports.items()}
        return self._endpoints

    def rest(self) -> N8nRest:
        rest = N8nRest(self.endpoints()["n8n"])
        rest.login(self.secret("n8n-owner", "email"), self.secret("n8n-owner", "password"))
        return rest

    def wait_http(self, url, accept, timeout=600):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    last = (response.status, response.read().decode(errors="replace"))
            except urllib.error.HTTPError as error:
                last = (error.code, error.read().decode(errors="replace"))
            except Exception as error:
                last = (None, str(error))
            if accept(*last):
                return last
            time.sleep(5)
        raise RuntimeError(f"{url} did not become ready: {last}")

    def setup_n8n(self):
        base = self.endpoints()["n8n"]
        self.wait_http(f"{base}/healthz/readiness", lambda status, body: status == 200)
        email, password = self.secret("n8n-owner", "email"), self.secret("n8n-owner", "password")
        rest = N8nRest(base)
        try:
            rest.login(email, password)
        except RuntimeError:
            rest.call(
                "POST",
                "/rest/owner/setup",
                {"email": email, "firstName": "Payables", "lastName": "Operations", "password": password},
            )
            rest.login(email, password)
        existing = {w["name"]: w["id"] for w in rest.call("GET", "/rest/workflows")}
        for path in self.WORKFLOW_FILES:
            definition = json.loads(path.read_text())
            name = definition["name"]
            if name not in existing:
                created = rest.call("POST", "/rest/workflows", {**definition, "active": False})
                existing[name] = created["id"]
                rest.call("POST", f"/rest/workflows/{created['id']}/activate", {"versionId": created["versionId"]})
            self.workflow_ids[name] = existing[name]
        self.workflow_id = self.workflow_ids[self.WORKFLOW_NAME]
        self.wait_webhook()

    def wait_webhook(self, timeout=300):
        self.wait_http(
            f"{self.endpoints()['n8n']}/webhook/{self.WEBHOOK_PATH}",
            lambda status, body: status is not None and "not registered for GET" in body,
            timeout=timeout,
        )

    def find_workflow_id(self, name: str, rest: N8nRest | None = None) -> str:
        if name not in self.workflow_ids:
            rest = rest or self.rest()
            self.workflow_ids[name] = next(w["id"] for w in rest.call("GET", "/rest/workflows") if w["name"] == name)
        return self.workflow_ids[name]

    def workflow(self, rest: N8nRest | None = None, name: str | None = None) -> dict:
        rest = rest or self.rest()
        workflow_id = self.find_workflow_id(name or self.WORKFLOW_NAME, rest)
        if name is None or name == self.WORKFLOW_NAME:
            self.workflow_id = workflow_id
        return rest.call("GET", f"/rest/workflows/{workflow_id}")

    # ------------------------------------------------------------------ workload
    def create_workload(self, **kwargs):
        pass

    def start_workload(self):
        if self.traffic is None:
            self.traffic = self.TRAFFIC(self.endpoints)
        self.traffic.start()
        self.wait_workload_started()

    def wait_workload_started(self):
        raise NotImplementedError

    def stop_workload(self):
        if self.traffic is not None:
            self.traffic.stop()

    # ------------------------------------------------------------------ inspection
    def psql(self, sql: str) -> list[list[str]]:
        out = self.command(
            "exec", "deploy/postgres", "--", "psql", "-U", "n8n", "-d", "n8n", "-AtF", "\t", "-c", sql, timeout=120
        )
        return [line.split("\t") for line in out.splitlines() if line]

    def portal(self, path):
        status, payload = http("GET", f"{self.endpoints()['portal']}{path}")
        if status != 200:
            raise RuntimeError(f"portal {path} answered {status}: {payload}")
        return payload

    def cancel_payout(self, payout_id, reason):
        return http("POST", f"{self.endpoints()['gateway']}/v1/payouts/{payout_id}/cancel", {"reason": reason})

    # ------------------------------------------------------------------ teardown
    def cleanup(self):
        self.stop_workload()
        self._endpoints = None
        self.kubectl.delete_namespace(self.namespace)

    def delete(self):
        self.cleanup()
