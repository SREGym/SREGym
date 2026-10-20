"""Reconcile per-hypervisor message-broker access with the rack inventory.

Runs on a schedule. Every rack publishes its host list, and the union of those
lists is the set of hypervisors allowed on the broker: hosts in the inventory
get a user, and broker users for hosts no longer in the inventory are deleted,
which also closes their connections. That is how a decommissioned or
compromised hypervisor is cut off.
"""

import base64
import glob
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

INVENTORY_DIR = os.environ.get("INVENTORY_DIR", "/inventory")
API = os.environ.get("BROKER_API", "http://rabbitmq:15672/api")
HOST_USER_PREFIX = "hv-"


def broker_password(key: bytes, host: str) -> str:
    return hmac.new(key, host.encode(), hashlib.sha256).hexdigest()[:32]


def call(method, path, body=None):
    credentials = f"{os.environ['BROKER_ADMIN_USER']}:{os.environ['BROKER_ADMIN_PASSWORD']}"
    request = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": "Basic " + base64.b64encode(credentials.encode()).decode(),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def inventory() -> dict[str, str]:
    """Return {host: rack} from every published rack inventory."""
    hosts = {}
    for path in sorted(glob.glob(os.path.join(INVENTORY_DIR, "*.json"))):
        with open(path) as handle:
            document = json.load(handle)
        for entry in document["hosts"]:
            hosts[entry["name"]] = document["rack"]
    return hosts


def main() -> None:
    with open("/etc/broker/host-key", "rb") as handle:
        key = handle.read().strip()
    allowed = inventory()
    users = {user["name"] for user in call("GET", "/users") if user["name"].startswith(HOST_USER_PREFIX)}
    racks = sorted(set(allowed.values()))
    print(f"inventory: {len(allowed)} hypervisors in {len(racks)} racks ({', '.join(racks)})")
    for host in sorted(allowed.keys() - users):
        name = urllib.parse.quote(host, safe="")
        call("PUT", f"/users/{name}", {"password": broker_password(key, host), "tags": "hypervisor"})
        call("PUT", f"/permissions/%2F/{name}", {"configure": ".*", "write": ".*", "read": ".*"})
        print(f"granted broker access: {host}")
    for host in sorted(users - allowed.keys()):
        try:
            call("DELETE", f"/users/{urllib.parse.quote(host, safe='')}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        print(f"revoked broker access: {host} (not in inventory)")
    print(
        f"reconciled: {len(allowed)} allowed, {len(allowed.keys() - users)} granted, {len(users - allowed.keys())} revoked"
    )


if __name__ == "__main__":
    main()
