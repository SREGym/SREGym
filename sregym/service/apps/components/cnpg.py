"""Install the PostgreSQL operator used by the stateful applications."""

import hashlib
import json
import urllib.request

VERSION = "1.30.1"
OPERATOR_IMAGE = f"ghcr.io/cloudnative-pg/cloudnative-pg:{VERSION}"
MANIFEST_URL = (
    f"https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.30/releases/cnpg-{VERSION}.yaml"
)
MANIFEST_SHA256 = "37237f145d8138256ea25ae830f87759255665ff08f8d552fdd8224a5ec032fb"


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
