"""Executable laboratory gaming services, scheduled as independent Nomad tasks.

There is no incident flag, artificial latency, or repair endpoint in this code.
Failures propagate through HTTP, Consul, Vault, PostgreSQL and Redis operations.
"""

import hashlib
import hmac
import json
import os
import random
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import psycopg
import redis
import requests

ROLE = os.environ["SERVICE"]
PORT = int(os.environ.get("NOMAD_PORT_http", "8080"))  # noqa: SIM112 -- native Nomad label
CONSUL = os.environ.get("CONSUL_HTTP_ADDR", "http://127.0.0.1:8500")
VAULT = os.environ["VAULT_ADDR"]
DBS = os.environ["DATABASE_URLS"].split(",")
CACHES = os.environ["CACHE_HOSTS"].split(",")
QUEUE = redis.Redis(host=os.environ["QUEUE_HOST"], decode_responses=True, socket_timeout=3)
COUNTS = Counter()
LOCK = threading.Lock()
SECRET = (0, "")
CONTEXT = threading.local()


def log(event, **fields):
    print(json.dumps({"time": time.time(), "service": ROLE, "event": event, **fields}), flush=True)


def http(url, method="GET", data=None, headers=None, timeout=3):
    r = requests.request(method, url, json=data, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json() if r.content else None


def discover(service):
    rows = http(f"{CONSUL}/v1/health/service/routing?passing=true&cached=true")
    if not rows:
        raise RuntimeError("no passing routing endpoints")
    item = random.choice(rows)
    router = f"http://{item['Service']['Address'] or item['Node']['Address']}:{item['Service']['Port']}"
    endpoints = http(router + f"/resolve?service={service}&tenant={getattr(CONTEXT, 'player', 0)}")["endpoints"]
    if not endpoints:
        raise RuntimeError(f"no routed endpoints for {service}")
    return random.choice(endpoints)


def call(service, path, data=None):
    start = time.monotonic()
    try:
        return http(discover(service) + path, "POST" if data is not None else "GET", data)
    except Exception as exc:
        log("dependency_error", dependency=service, path=path, error=str(exc), elapsed=time.monotonic() - start)
        raise


def db(player):
    return psycopg.connect(DBS[player % len(DBS)], connect_timeout=2, options="-c statement_timeout=2000")


def cache(player):
    return redis.Redis(host=CACHES[player % len(CACHES)], decode_responses=True, socket_timeout=2)


def signing_key():
    global SECRET
    if time.monotonic() > SECRET[0]:
        value = http(VAULT + "/v1/kv/platform", headers={"X-Vault-Token": os.environ["VAULT_TOKEN"]})
        SECRET = (time.monotonic() + 10, value["data"]["signing_key"])
    return SECRET[1]


def profile(player):
    client = cache(player)
    key = f"profile:{player}"
    item = client.get(key)
    if item:
        with LOCK:
            COUNTS["cache_hits"] += 1
        return json.loads(item)
    with LOCK:
        COUNTS["cache_misses"] += 1
    with db(player) as conn:
        row = conn.execute("SELECT name FROM players WHERE id=%s", (player,)).fetchone()
    if row is None:
        raise ValueError("unknown player")
    result = {"player": player, "name": row[0]}
    client.setex(key, 120, json.dumps(result))
    return result


def handle(path, data, query):
    player = int(data.get("player", query.get("player", [0])[0]))
    CONTEXT.player = player
    request_id = str(data.get("request_id", ""))
    if path == "/health":
        return {"process": "running", "service": ROLE}
    if path == "/metrics":
        with LOCK:
            return dict(COUNTS)
    if ROLE == "profiles":
        return profile(player)
    if ROLE == "identity":
        record = call("profiles", f"/profile?player={player}")
        signature = hmac.new(signing_key().encode(), str(player).encode(), hashlib.sha256).hexdigest()
        return {**record, "token": signature}
    if ROLE == "inventory":
        with db(player) as conn:
            row = conn.execute("SELECT coins FROM players WHERE id=%s", (player,)).fetchone()
            items = conn.execute("SELECT request_id,item FROM purchases WHERE player=%s", (player,)).fetchall()
        return {"player": player, "coins": row[0], "items": items}
    if ROLE == "assets":
        experience = player % 32
        client = cache(player)
        asset = client.get(f"asset:{experience}")
        if asset is None:
            with db(player) as conn:
                row = conn.execute("SELECT document FROM experiences WHERE id=%s", (experience,)).fetchone()
            asset = json.dumps(row[0])
            client.setex(f"asset:{experience}", 120, asset)
        return json.loads(asset)
    if ROLE == "catalog":
        return {"experience": player % 32, "asset": call("assets", f"/asset?player={player}")}
    if ROLE == "sessions":
        return call("persistence", "/session", data)
    if ROLE == "persistence":
        if not request_id:
            raise ValueError("request_id required")
        with db(player) as conn:
            if not data.get("lookup_only"):
                conn.execute(
                    "INSERT INTO sessions (request_id,player) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (request_id, player),
                )
            row = conn.execute("SELECT player FROM sessions WHERE request_id=%s", (request_id,)).fetchone()
        if row is None:
            return None
        if row[0] != player:
            raise ValueError("request_id belongs to another player")
        return {"request_id": request_id, "player": player}
    if ROLE == "allocator":
        existing = call("sessions", "/lookup", {**data, "lookup_only": True})
        if existing is not None:
            return existing
        # Real distributed lease and placement metadata. Locks expire if the
        # service cannot renew; allocations are not rewritten by a repair API.
        session = http(CONSUL + "/v1/session/create", "PUT", {"TTL": "30s", "Behavior": "delete", "Name": request_id})[
            "ID"
        ]
        try:
            key = f"games/{player % 32}/{request_id}"
            result = requests.put(
                CONSUL + "/v1/kv/" + key + "?acquire=" + session,
                data=json.dumps({"player": player, "node": os.environ.get("NOMAD_ALLOC_ID")}),
                timeout=3,
            )
            result.raise_for_status()
            if not result.json():
                raise RuntimeError("placement lock unavailable")
            return call("sessions", "/session", data)
        finally:
            http(CONSUL + "/v1/session/destroy/" + session, "PUT")
    if ROLE == "matchmaking":
        call("catalog", f"/experience?player={player}")
        return call("allocator", "/allocate", data)
    if ROLE == "economy":
        if not request_id:
            raise ValueError("request_id required")
        # Atomic purchase + durable outbox. Retries reuse the transaction ID;
        # a downstream timeout must never double-charge a player.
        with db(player) as conn:
            conn.execute("SELECT id FROM players WHERE id=%s FOR UPDATE", (player,))
            old = conn.execute("SELECT player FROM purchases WHERE request_id=%s", (request_id,)).fetchone()
            if old and old[0] != player:
                raise ValueError("request_id conflict")
            if not old:
                row = conn.execute(
                    "UPDATE players SET coins=coins-1 WHERE id=%s AND coins>0 RETURNING coins", (player,)
                ).fetchone()
                if row is None:
                    raise ValueError("insufficient funds")
                conn.execute("INSERT INTO purchases VALUES (%s,%s,%s)", (request_id, player, "experience-pass"))
                conn.execute("INSERT INTO outbox (request_id,player) VALUES (%s,%s)", (request_id, player))
        return {"request_id": request_id, "player": player, "charged": 1}
    if ROLE == "edge":
        admission = requests.get(CONSUL + "/v1/kv/platform/admission?raw", timeout=3)
        admission.raise_for_status()
        if player % 100 >= int(admission.text):
            raise RuntimeError("maintenance admission")
        identity = call("identity", "/login", data)
        placement = call("matchmaking", "/join", data)
        purchase = call("economy", "/purchase", data)
        inventory = call("inventory", f"/inventory?player={player}")
        return {"identity": identity, "session": placement, "purchase": purchase, "inventory": inventory}
    if ROLE == "telemetry":
        rows = http(CONSUL + "/v1/catalog/services")
        observations = {}
        for name in rows:
            if name in ("consul", "telemetry", "vault", "nomad", "nomad-client"):
                continue
            try:
                observations[name] = call(name, "/metrics")
            except Exception as exc:
                observations[name] = {"error": str(exc)}
        return observations
    raise ValueError(f"unknown endpoint {path}")


def publish():
    while True:
        try:
            # Credential access and discovery are real dependencies even for
            # background processing, so stopping player traffic does not stop it.
            signing_key()
            discover("economy")
            for dsn in DBS:
                with psycopg.connect(dsn, connect_timeout=2) as conn:
                    rows = conn.execute(
                        "SELECT request_id,player FROM outbox WHERE NOT published ORDER BY created_at LIMIT 50 FOR UPDATE SKIP LOCKED"
                    ).fetchall()
                    for rid, player in rows:
                        QUEUE.xadd("purchases", {"request_id": rid, "player": player})
                        conn.execute("UPDATE outbox SET published=true WHERE request_id=%s", (rid,))
                        with LOCK:
                            COUNTS["published"] += 1
        except Exception as exc:
            log("publish_error", error=str(exc))
        time.sleep(0.5)


def consume():
    consumer = os.environ.get("NOMAD_ALLOC_ID", str(uuid.uuid4()))
    while True:
        try:
            try:
                QUEUE.xgroup_create("purchases", "receipts", id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            signing_key()
            discover("inventory")
            reclaimed = QUEUE.xautoclaim("purchases", "receipts", consumer, 10000, "0-0", count=50)[1]
            batches = QUEUE.xreadgroup("receipts", consumer, {"purchases": ">"}, count=50, block=1000)
            messages = reclaimed + [message for _, batch in batches for message in batch]
            for mid, fields in messages:
                player = int(fields["player"])
                with db(player) as conn:
                    conn.execute(
                        "INSERT INTO receipts VALUES (%s,%s) ON CONFLICT DO NOTHING", (fields["request_id"], player)
                    )
                QUEUE.xack("purchases", "receipts", mid)
                with LOCK:
                    COUNTS["processed"] += 1
        except Exception as exc:
            log("consumer_error", error=str(exc))
            time.sleep(1)


def placement_inventory():
    """Publish fleet placement revisions into the shared service catalog.

    This is a laboratory control-plane client. Its update frequency is a real
    workload dimension, and all writes execute through Consul's Raft/FSM path.
    The registry continues to point at actual scheduled processes.
    """
    while True:
        try:
            for service in os.environ["ROUTE_SERVICES"].split(","):
                rows = http(CONSUL + "/v1/catalog/service/" + service)
                for row in rows:
                    revision = str(time.time_ns())
                    http(
                        CONSUL + "/v1/catalog/register",
                        "PUT",
                        {
                            "Node": row["Node"],
                            "Address": row["Address"],
                            "SkipNodeUpdate": True,
                            "Service": {
                                "ID": row["ServiceID"],
                                "Service": service,
                                "Address": row["ServiceAddress"],
                                "Port": row["ServicePort"],
                                "Tags": row["ServiceTags"],
                                "Meta": {**row.get("ServiceMeta", {}), "placement_revision": revision},
                            },
                        },
                    )
                    with LOCK:
                        COUNTS["placement_updates"] += 1
        except Exception as exc:
            log("placement_error", error=str(exc))
        time.sleep(float(os.environ.get("RECONCILE_SECONDS", "30")))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond({})

    def do_POST(self):
        self.respond(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"))

    def respond(self, data):
        start = time.monotonic()
        parsed = urlsplit(self.path)
        code = 200
        try:
            result = handle(parsed.path, data, parse_qs(parsed.query))
        except Exception as exc:
            code, result = 503, {"error": str(exc), "service": ROLE}
            log("request_error", path=parsed.path, request_id=data.get("request_id"), error=str(exc))
        with LOCK:
            if parsed.path not in ("/health", "/metrics"):
                COUNTS["requests"] += 1
                COUNTS["errors"] += int(code != 200)
                COUNTS["elapsed_ms"] += int(1000 * (time.monotonic() - start))
        payload = json.dumps(result).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    if ROLE in ("outbox", "analytics"):
        threading.Thread(target=publish if ROLE == "outbox" else consume, daemon=True).start()
    if ROLE == "placement":
        threading.Thread(target=placement_inventory, daemon=True).start()
    log("start", port=PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
