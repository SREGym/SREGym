"""OWASP Dependency-Track 5 (monolith topology) deployed from its Helm chart, with CI pipelines and a ticket bridge.

The chart (dependencytrack/helm-charts @ 5f43ccb, chart 2.8.0) is vendored next to
this file and checked against its SHA-256 before installation. Images are the
upstream ``ghcr.io/dependencytrack`` snapshot builds pinned by digest: the
api-server image is the one CI published for commit fd55decc (its
``org.opencontainers.image.revision`` label), the frontend the one published for
frontend commit 89924d6. PostgreSQL 14 and SeaweedFS (S3 file storage) are stock
images.

Workload (standard-library Python in a stock image, scripts in a ConfigMap):

* ``ci-pipelines`` uploads CycloneDX BOMs for the organisation's services the way
  CI jobs do (``PUT /api/v1/bom`` with an automation API key).
* ``violation-tracker`` receives Dependency-Track's outbound webhook
  notifications and opens one ticket per policy violation; it audits the
  portfolio periodically and reports violations that have no ticket.

Optional profile:

* ``intake``: four-hourly builds, release tags, the security team's internal
  advisories and the high-or-critical gate, on two pipeline runners, with main
  pipelines for pushes to main (the dependency update bot's merges), and the
  violation-tracker's dependency intake register: one intake ticket per
  vulnerable dependency a build introduces into a service version, from
  Dependency-Track's NEW_VULNERABLE_DEPENDENCY notifications.

Grader-side code (bootstrap, probes) lives in ``runtime/dt_ops.py`` and is piped
to ``python -`` in the ci-pipelines pod on stdin; it is never written to the
cluster.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import subprocess
import time
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.client_runner import PythonClientRunner
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.kubectl import KubeCtl

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
POSTGRES_IMAGE = "docker.io/library/postgres@sha256:bdbfe56435a1c58f706557a5db9494f7550a5db42f4bd796e0f7f9c19947161c"
SEAWEEDFS_IMAGE = (
    "docker.io/chrislusf/seaweedfs@sha256:b2e51562743be5637381a82eeeee4872eb2f86994b723d25a1bbd171855fe18d"
)
PYTHON_IMAGE = "python:3.12.13-alpine3.23"
WORKLOAD_SCRIPTS = ("ci_pipeline.py", "violation_tracker.py")
PROFILE_SCRIPTS = {
    "intake": (),
}
PROFILE_DESCRIPTIONS = {
    "intake": (
        " The pipelines run on two runners. They build every service on a four-hourly schedule; release tags start a "
        "service's release pipeline, and pushes to a repository's main branch (for example the dependency update "
        "bot's merges) start its main pipeline. The security team records its internal security advisories as "
        "internal vulnerabilities in Dependency-Track, and the policy `Vulnerabilities rated high or critical` fails "
        "the gate of affected builds. The violation-tracker also keeps the dependency intake register: one intake "
        "ticket per vulnerable dependency that a build introduces into a service version, opened from "
        "Dependency-Track's new vulnerable dependency notifications (/intake), and its audit reports vulnerable "
        "dependencies without an intake ticket."
    ),
}


class DependencyTrack(KubernetesCommands, Application):
    CHART = HERE / "dependency-track-2.8.0.tgz"
    CHART_SHA256 = "c7b89870fb0eff80a4c5408f58ef5592119497b364ac6e8b3b72abec62983fa7"
    VALUES = HERE / "values.yaml"
    RELEASE = "dt"
    API_DEPLOYMENT = "dt-api-server"
    FRONTEND_DEPLOYMENT = "dt-frontend"
    CI = "ci-pipelines"
    TRACKER = "violation-tracker"
    READY_TIMEOUT_S = 1200
    METADATA = HERE / "metadata.json"

    def __init__(self, profile: str | None = None):
        if profile not in (None, *PROFILE_SCRIPTS):
            raise ValueError(f"unknown Dependency-Track profile {profile!r}")
        super().__init__(self.METADATA)
        self.load_app_json()
        metadata = self.get_app_json()
        self.profile = profile
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"] + PROFILE_DESCRIPTIONS.get(profile, "")
        self.kubectl = KubeCtl()
        self.frontend_service = "dt-api-server"
        self.frontend_port = 8080
        self.helm_deploy = True
        #: Extra ``--set`` arguments of the current Helm release (configuration changes roll the api-server).
        self.helm_overrides: dict[str, str] = {}
        self.bootstrap: dict = {}
        self.admin_password = self.signing_secret = None

    # ------------------------------------------------------------------ manifests
    @staticmethod
    def _deployment(name, containers, volumes=(), strategy=None, labels=None, **pod):
        labels = {"app": name, **(labels or {})}
        spec = {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "containers": containers,
                    "volumes": list(volumes),
                    **pod,
                },
            },
        }
        if strategy:
            spec["strategy"] = strategy
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "labels": labels},
            "spec": spec,
        }

    @staticmethod
    def _service(name, port, port_name):
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {"selector": {"app": name}, "ports": [{"name": port_name, "port": port, "targetPort": port}]},
        }

    @staticmethod
    def _claim(name, size):
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}},
        }

    def backing_documents(self):
        security = {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        postgres = {
            "name": "postgres",
            "image": POSTGRES_IMAGE,
            "securityContext": {**security, "runAsUser": 70},
            "env": [
                {"name": "POSTGRES_DB", "value": "dtrack"},
                {"name": "POSTGRES_USER", "value": "dtrack"},
                {
                    "name": "POSTGRES_PASSWORD",
                    "valueFrom": {"secretKeyRef": {"name": "dt-db-creds", "key": "password"}},
                },
            ],
            "ports": [{"containerPort": 5432, "name": "postgres"}],
            "readinessProbe": {
                "exec": {"command": ["pg_isready", "-h", "127.0.0.1", "-U", "dtrack", "-d", "dtrack"]},
                "periodSeconds": 5,
                "timeoutSeconds": 3,
            },
            "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}},
            "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql"}],
        }
        seaweedfs = {
            "name": "seaweedfs",
            "image": SEAWEEDFS_IMAGE,
            "securityContext": {**security, "runAsUser": 1000},
            "args": ["server", "-s3", "-s3.config=/etc/seaweedfs/s3.json", "-dir=/data", "-master.telemetry=false"],
            "ports": [{"containerPort": 8333, "name": "s3"}],
            "readinessProbe": {"tcpSocket": {"port": 8333}, "periodSeconds": 5},
            "resources": {"requests": {"cpu": "150m", "memory": "256Mi"}, "limits": {"memory": "2Gi"}},
            "volumeMounts": [
                {"name": "data", "mountPath": "/data"},
                {"name": "cfg", "mountPath": "/etc/seaweedfs", "readOnly": True},
            ],
        }
        return [
            self._claim("postgres-data", "10Gi"),
            self._claim("seaweed-data", "10Gi"),
            self._deployment(
                "postgres",
                [postgres],
                [{"name": "data", "persistentVolumeClaim": {"claimName": "postgres-data"}}],
                strategy={"type": "Recreate"},
                securityContext={"runAsNonRoot": True, "fsGroup": 70, "seccompProfile": {"type": "RuntimeDefault"}},
            ),
            self._service("postgres", 5432, "postgres"),
            self._deployment(
                "seaweedfs",
                [seaweedfs],
                [
                    {"name": "data", "persistentVolumeClaim": {"claimName": "seaweed-data"}},
                    {"name": "cfg", "secret": {"secretName": "seaweedfs-s3-config"}},
                ],
                strategy={"type": "Recreate"},
                securityContext={"runAsNonRoot": True, "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            ),
            self._service("seaweedfs", 8333, "s3"),
        ]

    def workload_configmap(self):
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "pipeline-scripts"},
            "data": {
                name: (RUNTIME / name).read_text()
                for name in (*WORKLOAD_SCRIPTS, *PROFILE_SCRIPTS.get(self.profile, ()))
            },
        }

    def _python(self, name, script, env, port=None, cpu="50m", memory="256Mi"):
        container = {
            "name": name,
            "image": PYTHON_IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "command": ["python", "-u", f"/opt/pipeline/{script}"],
            "env": [{"name": "PYTHONUNBUFFERED", "value": "1"}, *env],
            "volumeMounts": [{"name": "scripts", "mountPath": "/opt/pipeline", "readOnly": True}],
            "resources": {"requests": {"cpu": cpu, "memory": "64Mi"}, "limits": {"memory": memory}},
        }
        if port:
            container["ports"] = [{"name": "http", "containerPort": port}]
            container["readinessProbe"] = {
                "httpGet": {"path": "/healthz", "port": port},
                "periodSeconds": 10,
                "timeoutSeconds": 5,
            }
        return container

    def tracker_documents(self):
        env = [
            {"name": "DTRACK_URL", "value": "http://dt-api-server:8080"},
            {"name": "DTRACK_API_KEY", "valueFrom": {"secretKeyRef": {"name": "tracker-api-key", "key": "key"}}},
            {
                "name": "SIGNING_SECRET",
                "valueFrom": {"secretKeyRef": {"name": "tracker-signing-secret", "key": "secret"}},
            },
            {"name": "AUDIT_INTERVAL", "value": "300"},
        ]
        if self.profile == "intake":
            env.append({"name": "INTAKE_TICKETS", "value": "true"})
        container = self._python("tracker", "violation_tracker.py", env, port=8080, cpu="100m", memory="512Mi")
        container["ports"][0]["name"] = "http-metrics"
        container["volumeMounts"].append({"name": "data", "mountPath": "/var/lib/tracker"})
        return [
            self._claim("tracker-data", "1Gi"),
            self._deployment(
                self.TRACKER,
                [container],
                [
                    {"name": "scripts", "configMap": {"name": "pipeline-scripts"}},
                    {"name": "data", "persistentVolumeClaim": {"claimName": "tracker-data"}},
                ],
                strategy={"type": "Recreate"},
                labels={"app.kubernetes.io/part-of": "security-tooling"},
            ),
            self._service(self.TRACKER, 8080, "http"),
        ]

    def ci_documents(self):
        env = [{"name": "DTRACK_URL", "value": "http://dt-api-server:8080"}]
        if self.profile == "intake":
            env += [
                {"name": "SCHEDULE_MODE", "value": "train"},
                {"name": "TRAIN_INTERVAL_SECONDS", "value": "14400"},
                {"name": "RUNNERS", "value": "2"},
            ]
        container = self._python("pipelines", "ci_pipeline.py", env, port=8080, cpu="100m")
        container["volumeMounts"].append({"name": "credentials", "mountPath": "/etc/dtrack", "readOnly": True})
        return [
            self._deployment(
                self.CI,
                [container],
                [
                    {"name": "scripts", "configMap": {"name": "pipeline-scripts"}},
                    {
                        "name": "credentials",
                        "secret": {
                            "secretName": "ci-api-key",
                            "optional": True,
                            "items": [{"key": "key", "path": "api-key"}],
                        },
                    },
                ],
                labels={"app.kubernetes.io/part-of": "ci"},
            ),
            self._service(self.CI, 8080, "http"),
        ]

    # ------------------------------------------------------------------ secrets
    def _secret_value(self, name, key):
        out = self.command("get", "secret", name, "--ignore-not-found", "-o", f"jsonpath={{.data.{key}}}")
        return base64.b64decode(out).decode() if out else None

    def _secret(self, name, data):
        return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "type": "Opaque", "stringData": data}

    def ensure_secrets(self):
        dbpw = self._secret_value("dt-db-creds", "password") or secrets.token_hex(24)
        s3key = self._secret_value("dt-s3-creds", "secretAccessKey") or secrets.token_hex(24)
        kek = self._secret_value("dt-kek", "kek") or base64.b64encode(secrets.token_bytes(32)).decode()
        admin = self._secret_value("dt-admin-credentials", "password") or secrets.token_urlsafe(18)
        signing = self._secret_value("tracker-signing-secret", "secret") or secrets.token_hex(32)
        s3cfg = {
            "identities": [
                {
                    "name": "dtrack",
                    "credentials": [{"accessKey": "dtrack", "secretKey": s3key}],
                    "actions": ["Admin", "Read", "Write", "List", "Tagging"],
                }
            ]
        }
        self.apply(
            [
                self._secret("dt-db-creds", {"username": "dtrack", "password": dbpw}),
                self._secret("dt-s3-creds", {"accessKeyId": "dtrack", "secretAccessKey": s3key}),
                self._secret("dt-kek", {"kek": kek}),
                self._secret("seaweedfs-s3-config", {"s3.json": json.dumps(s3cfg)}),
                self._secret("dt-admin-credentials", {"username": "admin", "password": admin}),
                self._secret("tracker-signing-secret", {"secret": signing}),
            ]
        )
        self.admin_password, self.signing_secret = admin, signing

    # ------------------------------------------------------------------ deployment
    def helm(self, *args, timeout=900):
        return subprocess.run(["helm", *args], capture_output=True, text=True, check=True, timeout=timeout).stdout

    def helm_upgrade(self, overrides: dict[str, str] | None = None, wait=False):
        """``helm upgrade --install`` with the vendored chart, values.yaml and the given --set overrides."""
        if overrides is not None:
            self.helm_overrides = dict(overrides)
        args = ["upgrade", "--install", self.RELEASE, str(self.CHART), "-n", self.namespace, "-f", str(self.VALUES)]
        for key, value in sorted(self.helm_overrides.items()):
            args += ["--set-string", f"{key}={value}"]
        if wait:
            args += ["--wait", "--timeout", f"{self.READY_TIMEOUT_S}s"]
        return self.helm(*args, timeout=self.READY_TIMEOUT_S + 60)

    def rollout(self, name, timeout=None):
        timeout = timeout or self.READY_TIMEOUT_S
        self.command("rollout", "status", f"deployment/{name}", f"--timeout={timeout}s", timeout=timeout + 30)

    def deploy(self):
        digest = hashlib.sha256(self.CHART.read_bytes()).hexdigest()
        if digest != self.CHART_SHA256:
            raise RuntimeError(f"Vendored chart {self.CHART.name} has digest {digest}, expected {self.CHART_SHA256}")
        self.kubectl.create_namespace_if_not_exist(self.namespace)
        self.ensure_secrets()
        self.apply(self.backing_documents())
        # The pipeline runner waits for its API key; it also hosts the grader-side bootstrap call.
        self.apply([self.workload_configmap(), *self.ci_documents()])
        self.rollout("postgres")
        self.rollout("seaweedfs")
        self.create_bucket()
        self.rollout(self.CI)
        self.wait_tcp("postgres", 5432)
        self.wait_tcp("seaweedfs", 8333)
        self.helm_upgrade({})
        # Bootstrap as soon as the API answers: the first-start NVD/KEV mirror runs must find their sources disabled.
        self.bootstrap = self.ops(
            f"bootstrap({self.admin_password!r}, {self.signing_secret!r}, "
            f"'http://{self.TRACKER}:8080/hooks/dependency-track')",
            timeout=1200,
        )
        self.rollout(self.API_DEPLOYMENT)
        self.rollout(self.FRONTEND_DEPLOYMENT)
        self.wait_api_ready()
        self.apply(
            [
                self._secret("secops-api-key", {"key": self.bootstrap["secops_key"]}),
                self._secret("tracker-api-key", {"key": self.bootstrap["tracker_key"]}),
                *self.tracker_documents(),
            ]
        )
        self.rollout(self.TRACKER)
        if self.profile == "intake":
            self.ops(
                f"configure_intake({self.admin_password!r}, 'http://{self.TRACKER}:8080/hooks/dependency-track')",
                timeout=600,
            )

    def create_bucket(self, attempts=30):
        for _ in range(attempts):
            try:
                out = self.command(
                    "exec",
                    "deploy/seaweedfs",
                    "--",
                    "sh",
                    "-c",
                    "echo 's3.bucket.create -name dtrack' | weed shell -master=localhost:9333 2>&1; "
                    "echo 's3.bucket.list' | weed shell -master=localhost:9333 2>&1",
                    timeout=120,
                )
                if "dtrack" in out.split("s3.bucket.list", 1)[-1]:
                    return
            except subprocess.CalledProcessError:
                pass
            time.sleep(5)
        raise RuntimeError("Could not create the dtrack bucket in SeaweedFS")

    def wait_tcp(self, host, port, timeout=300):
        """Wait until ``host:port`` accepts connections from inside the namespace (pod networking is up)."""
        probe = (
            "import socket,sys,time\n"
            f"end=time.time()+{timeout}\n"
            "while time.time()<end:\n"
            "    try:\n"
            f"        socket.create_connection(({host!r},{port}),timeout=3).close(); sys.exit(0)\n"
            "    except OSError: time.sleep(2)\n"
            "sys.exit(1)\n"
        )
        self.command("exec", "-i", self.pod_name(self.CI), "--", "python", "-", input_text=probe, timeout=timeout + 30)

    def wait_api_ready(self, timeout=600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                out = self.command(
                    "exec",
                    f"deploy/{self.API_DEPLOYMENT}",
                    "--",
                    "wget",
                    "-qO-",
                    "-T",
                    "5",
                    "http://127.0.0.1:9000/health/ready",
                    timeout=30,
                )
                if json.loads(out).get("status") == "UP":
                    return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
                pass
            time.sleep(5)
        raise RuntimeError("Dependency-Track api-server did not report ready")

    # ------------------------------------------------------------------ workload
    def create_workload(self, **kwargs):
        pass

    def start_workload(self):
        """Hand the pipelines their API key and wait for the first policy gate."""
        self.apply([self._secret("ci-api-key", {"key": self.bootstrap["ci_key"]})])
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if '"msg": "policy gate"' in self.command("logs", f"deploy/{self.CI}", "--tail=200"):
                return
            time.sleep(10)
        raise RuntimeError("The CI pipelines did not complete a run against Dependency-Track")

    def stop_workload(self):
        pass

    # ------------------------------------------------------------------ probes and helpers
    def ops(self, call: str, timeout: int = 300):
        """Evaluate ``call`` with the grader-side helpers in the ci-pipelines pod (source on stdin)."""
        pod = self.pod_name(self.CI)
        try:
            return PythonClientRunner(self.command, pod=pod).run(
                [RUNTIME / "dt_ops.py"], f"print(json.dumps({call}))", timeout=timeout
            )
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"ops {call[:80]} failed: {(error.stderr or '')[-1500:]}") from None

    def admin_call(self, name, *args, timeout=300, as_admin=False):
        """``ops`` for a dt_ops function that takes a Dependency-Track credential as its first argument.

        Reads and triage use the security operations team's API key (API-key requests are not
        recorded as logins); ``as_admin``, or a key that no longer works, logs in as admin instead.
        """
        key = None if as_admin else (self.bootstrap.get("secops_key") or self._secret_value("secops-api-key", "key"))
        if key:
            try:
                return self.ops(f"{name}({', '.join(repr(a) for a in (key, *args))})", timeout=timeout)
            except RuntimeError as error:
                if " -> 401" not in str(error) and " -> 403" not in str(error):
                    raise
        password = self.admin_password or self._secret_value("dt-admin-credentials", "password")
        return self.ops(f"{name}({', '.join(repr(a) for a in (password, *args))})", timeout=timeout)

    def pod_name(self, app):
        pods = json.loads(self.command("get", "pods", "-l", f"app={app}", "-o", "json"))["items"]
        live = [p for p in pods if not p["metadata"].get("deletionTimestamp") and p["status"].get("phase") == "Running"]
        if not live:
            raise RuntimeError(f"No running pod for app={app}")
        return live[0]["metadata"]["name"]

    def psql(self, sql: str, timeout=120) -> str:
        return self.command(
            "exec",
            "deploy/postgres",
            "--",
            "sh",
            "-c",
            'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U dtrack -d dtrack -tAX -F "|" -c "$0"',
            sql,
            timeout=timeout,
        )

    # ------------------------------------------------------------------ teardown
    def cleanup(self):
        subprocess.run(
            ["kubectl", "-n", self.namespace, "delete", "pods", "--all", "--grace-period=5", "--wait=false"],
            capture_output=True,
            timeout=120,
        )
        self.kubectl.delete_namespace(self.namespace)

    def delete(self):
        self.cleanup()
