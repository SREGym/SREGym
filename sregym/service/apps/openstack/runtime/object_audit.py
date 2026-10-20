"""Audit object durability and ring capacity for the grader.

Reads the object ring exactly as the ring service distributes it, and reads
every expected object back through the public API as its owning tenant,
comparing content checksums. Prints one JSON report.
"""

import hashlib
import json
import os
import sys
import tempfile
import urllib.request

import requests
from swift.common.ring import RingData

KEYSTONE = os.environ["OS_AUTH_URL"]
RING_SOURCE = os.environ.get("RING_SOURCE", "http://swift-rings.openstack.svc:8000")


def canary_body(project, index):
    return hashlib.sha256(f"{project}/canary-{index}".encode()).digest() * 128


def ring_devices():
    with tempfile.NamedTemporaryFile(suffix=".ring.gz") as handle:
        with urllib.request.urlopen(f"{RING_SOURCE}/object.ring.gz", timeout=20) as response:
            handle.write(response.read())
        handle.flush()
        ring = RingData.load(handle.name)
    return sorted((f"{d['ip']}/{d['device']}", d["weight"]) for d in ring.devs if d is not None)


def token_and_storage(account):
    body = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {"name": account["user"], "domain": {"id": "default"}, "password": account["password"]}
                },
            },
            "scope": {"project": {"name": account["project"], "domain": {"id": "default"}}},
        }
    }
    response = requests.post(f"{KEYSTONE}/auth/tokens", json=body, timeout=30)
    response.raise_for_status()
    catalog = response.json()["token"]["catalog"]
    url = next(
        e["url"] for s in catalog if s["type"] == "object-store" for e in s["endpoints"] if e["interface"] == "public"
    )
    return response.headers["X-Subject-Token"], url


def audit(canaries_per_tenant, writes):
    with open(os.environ.get("ACCOUNTS_FILE", "/etc/tenants/accounts.json")) as handle:
        accounts = json.load(handle)
    report = {"ring_devices": ring_devices(), "tenants": {}}
    for account in accounts:
        project = account["project"]
        token, url = token_and_storage(account)
        expected = [
            (f"canary-{i}", hashlib.sha256(canary_body(project, i)).hexdigest()) for i in range(canaries_per_tenant)
        ]
        expected += [tuple(w) for w in writes.get(project, [])]
        missing, corrupt = [], []
        for name, digest in expected:
            response = requests.get(f"{url}/records/{name}", headers={"X-Auth-Token": token}, timeout=30)
            if response.status_code != 200:
                missing.append(f"{name}: HTTP {response.status_code}")
            elif hashlib.sha256(response.content).hexdigest() != digest:
                corrupt.append(name)
        report["tenants"][project] = {"checked": len(expected), "missing": missing, "corrupt": corrupt}
    return report


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(audit(request["canaries_per_tenant"], request.get("writes", {}))))
