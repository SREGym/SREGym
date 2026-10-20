"""Tenant traffic for the OpenStack region, and the ledger that records it.

Each tenant signs in to its own project and, in a closed loop, lists its
servers, boots and deletes a short-lived server, or reboots one of its
long-lived ("pet") servers. With object storage enabled it also reads stored
objects (checking their content) and writes new ones. Every operation is
appended to a ledger with its start, end and outcome; acknowledged object
writes carry their checksum. The grader reads the ledger over HTTP; this
process never judges health itself.

``seed`` mode boots the pet servers and stores the canary objects once, before
the baseline. The loop never recreates either, so anything lost stays lost.
"""

import hashlib
import json
import os
import random
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

KEYSTONE = os.environ["OS_AUTH_URL"]
NOVA_VERSION = {"OpenStack-API-Version": "compute 2.79", "X-OpenStack-Nova-API-Version": "2.79"}
TIMEOUT = 30
BOOT_TIMEOUT_S = float(os.environ.get("BOOT_TIMEOUT_S", "90"))
REBOOT_TIMEOUT_S = float(os.environ.get("REBOOT_TIMEOUT_S", "60"))
LEDGER_PATH = os.environ.get("LEDGER_PATH", "/ledger/ledger.jsonl")
# Families that enable Swift also store and read objects.
OBJECT_STORAGE = os.environ.get("OBJECT_STORAGE") == "1"
CANARIES_PER_TENANT = int(os.environ.get("CANARIES_PER_TENANT", "0"))
CONTAINER = "records"

EPISODE = uuid.uuid4().hex
LEDGER: list[dict] = []
LOCK = threading.Lock()
STARTED = time.time()


def record(tenant, op, started, ok, error=None, **extra):
    entry = {"t": started, "end": time.time(), "tenant": tenant, "op": op, "ok": ok, "error": error, **extra}
    entry["latency_s"] = round(entry["end"] - started, 3)
    with LOCK:
        LEDGER.append(entry)
        with open(LEDGER_PATH, "a") as handle:
            handle.write(json.dumps(entry) + "\n")


class Tenant:
    def __init__(self, account):
        self.name = account["project"]
        self.account = account
        self.token = None
        self.token_at = 0.0
        self.catalog = {}

    def authenticate(self, force=False):
        if not force and self.token and time.time() - self.token_at < 900:
            return
        body = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.account["user"],
                            "domain": {"id": "default"},
                            "password": self.account["password"],
                        }
                    },
                },
                "scope": {"project": {"name": self.account["project"], "domain": {"id": "default"}}},
            }
        }
        response = requests.post(f"{KEYSTONE}/auth/tokens", json=body, timeout=TIMEOUT)
        response.raise_for_status()
        self.token = response.headers["X-Subject-Token"]
        self.token_at = time.time()
        for service in response.json()["token"]["catalog"]:
            for endpoint in service["endpoints"]:
                if endpoint["interface"] == "public":
                    self.catalog[service["type"]] = endpoint["url"].rstrip("/")

    def call(self, method, service, path, **kwargs):
        for attempt in (0, 1):
            self.authenticate(force=attempt == 1)
            headers = {"X-Auth-Token": self.token, **NOVA_VERSION, **kwargs.pop("headers", {})}
            response = requests.request(
                method, self.catalog[service] + path, headers=headers, timeout=TIMEOUT, **kwargs
            )
            if response.status_code != 401:
                return response
        return response

    def server(self, server_id):
        response = self.call("GET", "compute", f"/servers/{server_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()["server"]

    def servers(self, prefix):
        response = self.call("GET", "compute", "/servers/detail")
        response.raise_for_status()
        return [s for s in response.json()["servers"] if s["name"].startswith(prefix)]

    def boot(self, name, image, flavor):
        response = self.call(
            "POST",
            "compute",
            "/servers",
            json={"server": {"name": name, "imageRef": image, "flavorRef": flavor, "networks": "none"}},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"boot rejected: HTTP {response.status_code} {response.text[:200]}")
        return response.json()["server"]["id"]

    def wait(self, server_id, done, timeout_s):
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            last = self.server(server_id)
            verdict = done(last)
            if verdict is not None:
                return verdict, last
            time.sleep(2)
        return None, last


def describe(server):
    if server is None:
        return "missing"
    fault = (server.get("fault") or {}).get("message", "")
    return f"status={server['status']} task_state={server.get('OS-EXT-STS:task_state')} {fault}".strip()


def built(server):
    if server is None:
        return False
    if server["status"] == "ACTIVE":
        return True
    if server["status"] == "ERROR":
        return False
    return None


def settled(server):
    if server is None:
        return False
    if server["status"] == "ACTIVE" and not server.get("OS-EXT-STS:task_state"):
        return True
    if server["status"] == "ERROR":
        return False
    return None


def churn(tenant, image, flavor):
    started = time.time()
    try:
        server_id = tenant.boot(f"churn-{uuid.uuid4().hex[:8]}", image, flavor)
    except Exception as exc:
        record(tenant.name, "boot", started, False, str(exc)[:300])
        return
    verdict, server = tenant.wait(server_id, built, BOOT_TIMEOUT_S)
    record(tenant.name, "boot", started, verdict is True, None if verdict else describe(server)[:300], server=server_id)
    started = time.time()
    try:
        response = tenant.call("DELETE", "compute", f"/servers/{server_id}")
        if response.status_code not in (204, 404):
            raise RuntimeError(f"delete rejected: HTTP {response.status_code} {response.text[:200]}")
        verdict, server = tenant.wait(server_id, lambda s: True if s is None else None, BOOT_TIMEOUT_S)
        record(
            tenant.name,
            "delete",
            started,
            verdict is True,
            None if verdict else describe(server)[:300],
            server=server_id,
        )
    except Exception as exc:
        record(tenant.name, "delete", started, False, str(exc)[:300], server=server_id)


def reboot(tenant, rng):
    started = time.time()
    try:
        pets = tenant.servers("pet-")
        if not pets:
            record(tenant.name, "pet_reboot", started, False, "tenant has no pet servers")
            return
        pet = rng.choice(pets)
        response = tenant.call("POST", "compute", f"/servers/{pet['id']}/action", json={"reboot": {"type": "SOFT"}})
        if response.status_code >= 400:
            raise RuntimeError(f"reboot rejected: HTTP {response.status_code} {response.text[:200]}")
        verdict, server = tenant.wait(pet["id"], settled, REBOOT_TIMEOUT_S)
        record(
            tenant.name,
            "pet_reboot",
            started,
            verdict is True,
            None if verdict else describe(server)[:300],
            server=pet["id"],
        )
    except Exception as exc:
        record(tenant.name, "pet_reboot", started, False, str(exc)[:300])


def listing(tenant):
    started = time.time()
    try:
        tenant.servers("")
        record(tenant.name, "list", started, True)
    except Exception as exc:
        record(tenant.name, "list", started, False, str(exc)[:300])


def canary_body(project, index):
    """Content of a pre-incident object, derived from its name so any copy can be checked."""
    return hashlib.sha256(f"{project}/canary-{index}".encode()).digest() * 128


def object_get(tenant, rng, writes):
    """Read a stored object and check its content: a canary, or one of this tenant's acknowledged writes."""
    started = time.time()
    if writes and rng.random() < 0.3:
        name, digest = rng.choice(writes)
    else:
        index = rng.randrange(CANARIES_PER_TENANT)
        name, digest = f"canary-{index}", hashlib.sha256(canary_body(tenant.name, index)).hexdigest()
    try:
        response = tenant.call("GET", "object-store", f"/{CONTAINER}/{name}")
        if response.status_code != 200:
            raise RuntimeError(f"object read failed: HTTP {response.status_code}")
        if hashlib.sha256(response.content).hexdigest() != digest:
            raise RuntimeError("object read returned different content")
        record(tenant.name, "object_get", started, True, object=name)
    except Exception as exc:
        record(tenant.name, "object_get", started, False, str(exc)[:300], object=name)


def object_put(tenant, writes):
    started = time.time()
    name = f"write-{uuid.uuid4().hex[:12]}"
    body = os.urandom(2048)
    digest = hashlib.sha256(body).hexdigest()
    try:
        response = tenant.call("PUT", "object-store", f"/{CONTAINER}/{name}", data=body)
        if response.status_code != 201:
            raise RuntimeError(f"object write failed: HTTP {response.status_code}")
        writes.append((name, digest))
        # An acknowledged write: grading checks it is still readable after recovery.
        record(tenant.name, "object_put", started, True, object=name, sha256=digest)
    except Exception as exc:
        record(tenant.name, "object_put", started, False, str(exc)[:300], object=name)


def tenant_loop(account, image, flavor, think_time_s, seed):
    tenant = Tenant(account)
    rng = random.Random(f"{seed}:{account['project']}")
    writes = []
    # (cumulative probability, operation)
    if OBJECT_STORAGE:
        mix = [(0.2, "churn"), (0.4, "reboot"), (0.45, "list"), (0.8, "object_get"), (1.0, "object_put")]
    else:
        mix = [(0.4, "churn"), (0.85, "reboot"), (1.0, "list")]
    while True:
        roll = rng.random()
        operation = next(op for bound, op in mix if roll < bound)
        if operation == "churn":
            churn(tenant, image, flavor)
        elif operation == "reboot":
            reboot(tenant, rng)
        elif operation == "list":
            listing(tenant)
        elif operation == "object_get":
            object_get(tenant, rng, writes)
        else:
            object_put(tenant, writes)
        time.sleep(think_time_s * (0.5 + rng.random()))


class LedgerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/status":
            with LOCK:
                latest = LEDGER[-1]["end"] if LEDGER else None
                payload = {"episode": EPISODE, "started": STARTED, "latest_end": latest, "records": len(LEDGER)}
        elif url.path == "/ledger":
            since = float(query.get("since", ["0"])[0])
            until = float(query.get("until", ["inf"])[0])
            with LOCK:
                payload = {"episode": EPISODE, "records": [e for e in LEDGER if since <= e["t"] <= until]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def load_accounts():
    with open(os.environ.get("ACCOUNTS_FILE", "/etc/tenants/accounts.json")) as handle:
        return json.load(handle)


def resolve(tenant):
    images = tenant.call("GET", "image", "/v2/images?name=cirros").json()["images"]
    flavors = tenant.call("GET", "compute", "/flavors").json()["flavors"]
    return images[0]["id"], next(f["id"] for f in flavors if f["name"] == "m1.small")


def seed(pets_per_tenant):
    """Boot every tenant's pets, one at a time and interleaved across tenants.

    Booting sequentially lets the scheduler see each placement before the next,
    so pets spread across hypervisors and every rack holds some tenants' state.
    Idempotent: existing pets are kept.
    """
    tenants = [Tenant(account) for account in load_accounts()]
    image, flavor = resolve(tenants[0])
    existing = {tenant.name: {s["name"] for s in tenant.servers("pet-")} for tenant in tenants}
    for index in range(pets_per_tenant):
        for tenant in tenants:
            name = f"pet-{tenant.name}-{index}"
            if name in existing[tenant.name]:
                continue
            verdict, server = tenant.wait(tenant.boot(name, image, flavor), settled, 300)
            if not verdict:
                raise RuntimeError(f"pet {name} did not become ACTIVE: {describe(server)}")
    if OBJECT_STORAGE:
        for tenant in tenants:
            tenant.call("PUT", "object-store", f"/{CONTAINER}").raise_for_status()
            for index in range(CANARIES_PER_TENANT):
                name = f"canary-{index}"
                if tenant.call("HEAD", "object-store", f"/{CONTAINER}/{name}").status_code == 200:
                    continue
                response = tenant.call(
                    "PUT", "object-store", f"/{CONTAINER}/{name}", data=canary_body(tenant.name, index)
                )
                if response.status_code != 201:
                    raise RuntimeError(f"canary {tenant.name}/{name} write failed: HTTP {response.status_code}")
    print(json.dumps({tenant.name: sorted(s["id"] for s in tenant.servers("pet-")) for tenant in tenants}))


def serve():
    accounts = load_accounts()
    image, flavor = resolve(Tenant(accounts[0]))
    think_time_s = float(os.environ.get("THINK_TIME_S", "2"))
    seed_value = os.environ.get("WORKLOAD_SEED", "0")
    for account in accounts:
        threading.Thread(
            target=tenant_loop, args=(account, image, flavor, think_time_s, seed_value), daemon=True
        ).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), LedgerHandler).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["seed"]:
        seed(int(sys.argv[2]))
    else:
        serve()
