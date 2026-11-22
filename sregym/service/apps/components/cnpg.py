"""Install the PostgreSQL operator used by the stateful applications."""

import hashlib
import json
import urllib.request
from collections.abc import Callable

VERSION = "1.30.1"
OPERATOR_IMAGE = f"ghcr.io/cloudnative-pg/cloudnative-pg:{VERSION}"
MANIFEST_URL = (
    f"https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.30/releases/cnpg-{VERSION}.yaml"
)
MANIFEST_SHA256 = "37237f145d8138256ea25ae830f87759255665ff08f8d552fdd8224a5ec032fb"


class PostgresCluster:
    """CNPG resource and SQL operations without application-specific configuration."""

    def __init__(self, command: Callable, name: str, database: str):
        self.command = command
        self.name = name
        self.database = database

    def document(self, spec: dict) -> dict:
        return {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "metadata": {"name": self.name},
            "spec": spec,
        }

    def get(self) -> dict:
        return json.loads(self.command("get", "cluster.postgresql.cnpg.io", self.name, "-o", "json"))

    def pods(self) -> list[dict]:
        pods = json.loads(self.command("get", "pods", "-l", f"cnpg.io/cluster={self.name}", "-o", "json"))["items"]
        return [
            pod
            for pod in pods
            if not pod["metadata"].get("deletionTimestamp")
            and any(container["name"] == "postgres" for container in pod["spec"]["containers"])
        ]

    def sql(self, statement: str, pod: str | None = None) -> str:
        pod = pod or self.get()["status"]["currentPrimary"]
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
            self.database,
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            input_text=statement,
            timeout=45,
        ).strip()


def ensure_operator(command):
    """Reuse the pinned operator, or install it during application setup.

    Conductor captures the cluster baseline before calling app.deploy(). Its
    normal reconciliation removes newly installed operator resources after the
    application and its volumes are gone. Existing installations are not upgraded.
    """
    deployments = json.loads(command("get", "deployments", "--all-namespaces", "-o", "json"))["items"]
    operators = [
        d
        for d in deployments
        if any("cloudnative-pg/cloudnative-pg" in c["image"] for c in d["spec"]["template"]["spec"]["containers"])
    ]
    if operators:
        if (
            len(operators) != 1
            or operators[0]["metadata"]["namespace"] != "cnpg-system"
            or operators[0]["metadata"]["name"] != "cnpg-controller-manager"
            or not any(c["image"] == OPERATOR_IMAGE for c in operators[0]["spec"]["template"]["spec"]["containers"])
        ):
            raise RuntimeError(f"A different CloudNativePG installation exists; this application requires {VERSION}")
    else:
        crds = json.loads(command("get", "crd", "-o", "json"))["items"]
        if any(c["metadata"]["name"].endswith(".postgresql.cnpg.io") for c in crds):
            raise RuntimeError("CloudNativePG CRDs exist without the expected operator; repair that installation first")
        with urllib.request.urlopen(MANIFEST_URL, timeout=60) as response:
            manifest = response.read()
        if hashlib.sha256(manifest).hexdigest() != MANIFEST_SHA256:
            raise RuntimeError("CloudNativePG manifest checksum does not match the pinned release")
        command("apply", "-n", "cnpg-system", "--server-side", "-f", "-", input_text=manifest.decode(), timeout=180)
    command(
        "rollout",
        "status",
        "deployment/cnpg-controller-manager",
        "-n",
        "cnpg-system",
        "--timeout=600s",
        timeout=620,
    )
