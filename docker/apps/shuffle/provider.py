"""Payment provider simulation. Mounted in khaos; callers see only its HTTP API."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path("/data/provider.sqlite")
RETENTION = 30  # Scaled-down key retention for a bounded benchmark run.
DOCS = f"""Payment provider API (benchmark clock)
POST /refunds: JSON {{"order_id": string, "cents": 100, "revision": integer,
"sandbox": optional boolean}} with Idempotency-Key. A real refund credits the order's
customer balance; this provider does not enforce a per-order credit ceiling.
The shop uses the globally unique producer revision as refund:<revision>.
The key binds to the first execution for {RETENTION} seconds. A repeated key returns
the first status and body even if the later request body differs. Once the key
expires, a repeated POST is a new credit. A 503 or timeout does not establish
whether a credit settled. Completed
outcomes, including errors, are cached for the key during retention. Inspect
the ledger if the result is uncertain. sandbox:true exercises the same key
behavior without changing the real ledger; sandbox keys and credits are
separate from real ones.
GET /ledger lists real refunds and adjustments, including keys and revisions.
GET /refunds/<id> retrieves one real refund. GET /status/history lists provider
degradation windows; it does not identify which requests committed.
POST /adjustments: JSON {{"refund_id": string}} with Idempotency-Key reverses
one real refund exactly once. GET /health returns 200.
"""


@contextmanager
def ledger():
    db = sqlite3.connect(DATA, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield db
    finally:
        db.close()


def initialize():
    DATA.parent.mkdir(exist_ok=True)
    with ledger() as db:
        db.executescript("""PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS refunds(id TEXT PRIMARY KEY,order_id TEXT,cents INT,
            revision INTEGER,key TEXT,created REAL,adjustment_id TEXT);
        CREATE TABLE IF NOT EXISTS requests(scope TEXT,key TEXT,status INT,body TEXT,expires REAL,
            PRIMARY KEY(scope,key));
        CREATE TABLE IF NOT EXISTS adjustments(id TEXT PRIMARY KEY,refund_id TEXT UNIQUE,key TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS disruptions(start REAL,end REAL,remaining INT,unpaid INT NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS ambiguous(order_id TEXT,key TEXT,paid INT,created REAL);
        """)


def disruption(seconds=10, count=3, unpaid=0):
    """Answer the next count real refunds with 503; the last unpaid of them are not credited."""
    now = time.time()
    with ledger() as db:
        db.execute(
            "INSERT INTO disruptions(start,end,remaining,unpaid) VALUES (?,?,?,?)", (now, now + seconds, count, unpaid)
        )


def post(path, payload, key, now=None):
    now = time.time() if now is None else now
    scope = ("sandbox" if payload.get("sandbox") else "real") + path
    if not key:
        return 400, {"error": "Idempotency-Key required"}
    with ledger() as db:
        cached = db.execute("SELECT status,body,expires FROM requests WHERE scope=? AND key=?", (scope, key)).fetchone()
        if cached and cached["expires"] > now:
            return cached["status"], json.loads(cached["body"])
        if path == "/refunds":
            if (
                not isinstance(payload.get("revision"), int)
                or payload.get("cents") != 100
                or not payload.get("order_id")
            ):
                return 400, {"error": "order_id, cents=100, integer revision required"}
            refund_id = "ref_" + uuid.uuid4().hex
            body = {
                "id": refund_id,
                "order_id": payload["order_id"],
                "cents": 100,
                "revision": payload["revision"],
                "key": key,
            }
            status = 200
            if not payload.get("sandbox"):
                db.execute(
                    "INSERT INTO refunds VALUES (?,?,?,?,?,?,NULL)",
                    (refund_id, payload["order_id"], 100, payload["revision"], key, now),
                )
                window = db.execute(
                    "SELECT rowid,remaining,unpaid FROM disruptions WHERE start<=? AND end>? "
                    "AND remaining>0 ORDER BY start DESC LIMIT 1",
                    (now, now),
                ).fetchone()
                if window:
                    paid = window["remaining"] > window["unpaid"]
                    if not paid:
                        db.execute("DELETE FROM refunds WHERE id=?", (refund_id,))
                    db.execute("UPDATE disruptions SET remaining=remaining-1 WHERE rowid=?", (window["rowid"],))
                    db.execute("INSERT INTO ambiguous VALUES (?,?,?,?)", (payload["order_id"], key, paid, now))
                    status, body = 503, {"error": "provider temporarily unavailable"}
        elif path == "/adjustments":
            if payload.get("sandbox"):
                return 400, {"error": "adjustments are not available in sandbox"}
            refund_id = payload.get("refund_id")
            row = db.execute("SELECT adjustment_id FROM refunds WHERE id=?", (refund_id,)).fetchone()
            if row is None:
                return 404, {"error": "refund not found"}
            if row["adjustment_id"]:
                return 409, {"error": "already adjusted", "adjustment_id": row["adjustment_id"]}
            adjustment_id = "adj_" + uuid.uuid4().hex
            body = {"id": adjustment_id, "refund_id": refund_id}
            db.execute("INSERT INTO adjustments VALUES (?,?,?,?)", (adjustment_id, refund_id, key, now))
            db.execute("UPDATE refunds SET adjustment_id=? WHERE id=?", (adjustment_id, refund_id))
            status = 200
        else:
            return 404, {"error": "unknown endpoint"}
        db.execute(
            "INSERT INTO requests VALUES (?,?,?,?,?) ON CONFLICT(scope,key) DO UPDATE SET "
            "status=excluded.status,body=excluded.body,expires=excluded.expires",
            (scope, key, status, json.dumps(body), now + RETENTION),
        )
        return status, body


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, body):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if isinstance(body, str) else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.respond(200, {"ok": True})
        if self.path == "/docs":
            return self.respond(200, DOCS)
        with ledger() as db:
            if self.path == "/ledger":
                return self.respond(
                    200,
                    {
                        "refunds": [dict(r) for r in db.execute("SELECT * FROM refunds ORDER BY created,id")],
                        "adjustments": [dict(r) for r in db.execute("SELECT * FROM adjustments ORDER BY created,id")],
                    },
                )
            if self.path == "/status/history":
                return self.respond(
                    200,
                    {
                        "retention_seconds": RETENTION,
                        "disruptions": [
                            dict(r) for r in db.execute("SELECT start,end FROM disruptions ORDER BY start")
                        ],
                    },
                )
            if self.path.startswith("/refunds/"):
                row = db.execute("SELECT * FROM refunds WHERE id=?", (self.path.rsplit("/", 1)[1],)).fetchone()
                return self.respond(200 if row else 404, dict(row) if row else {"error": "refund not found"})
        self.respond(404, {"error": "unknown endpoint"})

    def do_POST(self):
        try:
            payload = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))
            status, body = post(self.path, payload, self.headers.get("Idempotency-Key", ""))
        except (ValueError, TypeError, sqlite3.Error) as exc:
            status, body = 400, {"error": type(exc).__name__}
        self.respond(status, body)


def serve():
    initialize()
    ThreadingHTTPServer(("0.0.0.0", 8081), Provider).serve_forever()
