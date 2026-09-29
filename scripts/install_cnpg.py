"""Install the pinned PostgreSQL operator before capturing a benchmark baseline.

Run inside the dedicated DinD cluster. Existing installations are reused only
when they match the expected image; this command does not upgrade other clusters.
"""

import hashlib
import json
import subprocess
import urllib.request

VERSION = "1.30.1"
IMAGE = f"ghcr.io/cloudnative-pg/cloudnative-pg:{VERSION}"
URL = f"https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.30/releases/cnpg-{VERSION}.yaml"
SHA256 = "37237f145d8138256ea25ae830f87759255665ff08f8d552fdd8224a5ec032fb"


def install():
    existing = subprocess.run(
        ["kubectl", "get", "deployments", "-A", "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    operators = [
        d
        for d in json.loads(existing.stdout)["items"]
        if any("cloudnative-pg/cloudnative-pg" in c["image"] for c in d["spec"]["template"]["spec"]["containers"])
    ]
    if operators:
        if (
            len(operators) != 1
            or operators[0]["metadata"]["namespace"] != "cnpg-system"
            or not any(c["image"] == IMAGE for c in operators[0]["spec"]["template"]["spec"]["containers"])
        ):
            raise RuntimeError("A different CloudNativePG installation exists; use a fresh DinD cluster")
    else:
        crd = subprocess.run(
            ["kubectl", "get", "crd", "clusters.postgresql.cnpg.io", "--ignore-not-found", "-o", "name"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        if crd.stdout.strip():
            raise RuntimeError("CloudNativePG CRDs already exist without the expected operator; use a fresh cluster")
        with urllib.request.urlopen(URL, timeout=60) as response:
            manifest = response.read()
        if hashlib.sha256(manifest).hexdigest() != SHA256:
            raise RuntimeError("CloudNativePG manifest checksum changed")
        subprocess.run(["kubectl", "apply", "--server-side", "-f", "-"], input=manifest, check=True, timeout=180)
    subprocess.run(
        ["kubectl", "rollout", "status", "deployment/cnpg-controller-manager", "-n", "cnpg-system", "--timeout=600s"],
        check=True,
        timeout=620,
    )


if __name__ == "__main__":
    install()
