import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DB = os.environ.get("GATEWAY_DB", "/data/payouts.db")
PORT = int(os.environ.get("PORT", "8080"))
SETTLEMENT_HOURS = float(os.environ.get("SETTLEMENT_HOURS", "24"))
#: Seconds an account check takes at the verification service, "min-max".
ACCOUNT_CHECK_SECONDS = [float(x) for x in os.environ.get("ACCOUNT_CHECK_SECONDS", "130-180").split("-")]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")
lock = threading.Lock()


def stamp(moment=None):
    moment = moment or datetime.now(UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


db = sqlite3.connect(DB, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS payouts (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, reference TEXT, beneficiary TEXT,
        iban TEXT, amount TEXT, currency TEXT, status TEXT, created_at TEXT, settles_at TEXT,
        settled_at TEXT, cancelled_at TEXT, cancel_reason TEXT
    );
    CREATE INDEX IF NOT EXISTS payouts_reference ON payouts(reference);
    CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, payout_id TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS batches (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, reference TEXT, callback_url TEXT, payments INTEGER,
        total TEXT, status TEXT, created_at TEXT, reported_at TEXT, callback_status TEXT
    );
    CREATE TABLE IF NOT EXISTS batch_payouts (batch_id TEXT, payout_id TEXT);
    CREATE TABLE IF NOT EXISTS accounts (iban TEXT PRIMARY KEY, holder TEXT);
    CREATE TABLE IF NOT EXISTS account_checks (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, reference TEXT, iban TEXT, name TEXT,
        result TEXT, holder TEXT, created_at TEXT, ready_at REAL, served_at TEXT, polled_at TEXT
    );
    """
)
for statement in ("ALTER TABLE payouts ADD COLUMN batch_id TEXT",):
    try:  # noqa: SIM105
        db.execute(statement)
    except sqlite3.OperationalError:
        pass


def payout(payout_id):
    row = db.execute("SELECT * FROM payouts WHERE id = ?", (payout_id,)).fetchone()
    return dict(row) if row else None


def public(row):
    row = dict(row)
    row.pop("seq", None)
    return row


def insert_payout(body, batch_id=None):
    """Insert one scheduled payout (caller holds the lock and commits)."""
    created = datetime.now(UTC)
    cursor = db.execute(
        "INSERT INTO payouts (reference, beneficiary, iban, amount, currency, status, created_at, settles_at, batch_id)"
        " VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)",
        (
            body["reference"],
            body["beneficiary"],
            body["iban"],
            str(body["amount"]),
            body.get("currency", "EUR"),
            stamp(created),
            stamp(created + timedelta(hours=SETTLEMENT_HOURS)),
            batch_id,
        ),
    )
    payout_id = f"PO-{cursor.lastrowid:06d}"
    db.execute("UPDATE payouts SET id = ? WHERE seq = ?", (payout_id, cursor.lastrowid))
    earlier = [
        r["id"]
        for r in db.execute(
            "SELECT id FROM payouts WHERE reference = ? AND id != ? AND status != 'cancelled' ORDER BY seq",
            (body["reference"], payout_id),
        )
    ]
    return payout(payout_id), earlier


def log_payout(row, earlier):
    log.info(
        "payout %s scheduled: %s %s to %s (%s), reference %s, settles %s",
        row["id"],
        row["amount"],
        row["currency"],
        row["beneficiary"],
        row["iban"],
        row["reference"],
        row["settles_at"],
    )
    if earlier:
        log.warning(
            "payout %s: reference %s already has payout %s (possible duplicate payment)",
            row["id"],
            row["reference"],
            ", ".join(earlier),
        )


def create(body, key=None):
    missing = [k for k in ("reference", "beneficiary", "iban", "amount") if not body.get(k)]
    if missing:
        return 400, {"error": f"missing {', '.join(missing)}"}
    with lock:
        if key:
            known = db.execute("SELECT payout_id FROM idempotency WHERE key = ?", (key,)).fetchone()
            if known is not None:
                row = payout(known["payout_id"])
                log.info("payout request with idempotency key %s answered with existing payout %s", key, row["id"])
                return 200, public(row)
        row, earlier = insert_payout(body)
        if key:
            db.execute(
                "INSERT INTO idempotency (key, payout_id, created_at) VALUES (?, ?, ?)", (key, row["id"], stamp())
            )
        db.commit()
    log_payout(row, earlier)
    return 201, public(row)


# ---------------------------------------------------------------- payment batches
def create_batch(body):
    payments = body.get("payments")
    if not isinstance(payments, list) or not payments:
        return 400, {"error": "payments must be a non-empty list"}
    for i, item in enumerate(payments):
        missing = [k for k in ("reference", "beneficiary", "iban", "amount") if not (item or {}).get(k)]
        if missing:
            return 400, {"error": f"payment {i}: missing {', '.join(missing)}"}
    created, logged = stamp(), []
    with lock:
        cursor = db.execute(
            "INSERT INTO batches (reference, callback_url, payments, total, status, created_at) "
            "VALUES (?, ?, ?, ?, 'received', ?)",
            (
                str(body.get("reference") or ""),
                body.get("callback_url"),
                len(payments),
                f"{sum(float(p['amount']) for p in payments):.2f}",
                created,
            ),
        )
        batch_id = f"B-{cursor.lastrowid:05d}"
        db.execute("UPDATE batches SET id = ? WHERE seq = ?", (batch_id, cursor.lastrowid))
        for item in payments:
            row, earlier = insert_payout(item, batch_id)
            db.execute("INSERT INTO batch_payouts (batch_id, payout_id) VALUES (?, ?)", (batch_id, row["id"]))
            logged.append((row, earlier))
        db.commit()
        batch = get_batch(batch_id)
    log.info(
        "batch %s received (reference %s): %d payments, %s %s",
        batch_id,
        batch["reference"],
        batch["payments"],
        batch["total"],
        payments[0].get("currency", "EUR"),
    )
    for row, earlier in logged:
        log_payout(row, earlier)
    return 201, batch


def get_batch(batch_id):
    row = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        return None
    batch = public(row)
    batch["payout_ids"] = [
        r["payout_id"] for r in db.execute("SELECT payout_id FROM batch_payouts WHERE batch_id = ?", (batch_id,))
    ]
    return batch


def list_batches(query):
    sql, args, clauses = "SELECT * FROM batches", [], []
    if query.get("status"):
        clauses.append("status = ?")
        args.append(query["status"][0])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(int(query.get("limit", ["200"])[0]))
    return 200, [public(r) for r in db.execute(sql, args)]


def report_batch(batch_id, body):
    """Clearing has processed the batch: store its status and deliver the status report to the callback."""
    status = body.get("status") or "accepted"
    with lock:
        batch = get_batch(batch_id)
        if batch is None:
            return 404, {"error": f"unknown batch {batch_id}"}
        if batch["status"] != "received":
            return 409, {"error": f"batch {batch_id} is {batch['status']}"}
        db.execute("UPDATE batches SET status = ?, reported_at = ? WHERE id = ?", (status, stamp(), batch_id))
        db.commit()
    report = {
        "batch_id": batch_id,
        "reference": batch["reference"],
        "status": status,
        "payments": batch["payments"],
        "accepted": batch["payments"] if status == "accepted" else 0,
        "total": batch["total"],
    }
    code = None
    if batch["callback_url"]:
        request = urllib.request.Request(
            batch["callback_url"],
            data=json.dumps(report).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "northwind-bank-notifications/2.3"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
        except Exception as error:  # noqa: BLE001
            code = f"error: {error}"
        with lock:
            db.execute("UPDATE batches SET callback_status = ? WHERE id = ?", (str(code), batch_id))
            db.commit()
        if code == 200:
            log.info("batch %s %s; status report delivered to the callback (HTTP 200)", batch_id, status)
        else:
            log.warning("batch %s %s; status report to the callback answered %s", batch_id, status, code)
    return 200, {**report, "callback_status": code}


# ---------------------------------------------------------------- account verification
def register_account(body):
    if not body.get("iban") or not body.get("holder"):
        return 400, {"error": "iban and holder are required"}
    with lock:
        db.execute(
            "INSERT INTO accounts (iban, holder) VALUES (?, ?) ON CONFLICT(iban) DO UPDATE SET holder = excluded.holder",
            (body["iban"], body["holder"]),
        )
        db.commit()
    return 200, {"iban": body["iban"], "holder": body["holder"]}


def normalise(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def check_result(iban, name):
    row = db.execute("SELECT holder FROM accounts WHERE iban = ?", (iban,)).fetchone()
    if row is None:
        return "no_match", None
    holder, given = normalise(row["holder"]), normalise(name)
    if holder == given:
        return "match", row["holder"]
    if holder and given and (holder.startswith(given[:6]) or given.startswith(holder[:6])):
        return "close_match", row["holder"]
    return "no_match", row["holder"]


def create_check(body):
    if not body.get("iban") or not body.get("name"):
        return 400, {"error": "iban and name are required"}
    created = time.time()
    with lock:
        result, holder = check_result(body["iban"], body["name"])
        cursor = db.execute(
            "INSERT INTO account_checks (reference, iban, name, result, holder, created_at, ready_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                body.get("reference"),
                body["iban"],
                body["name"],
                result,
                holder,
                stamp(),
                created + random.uniform(*ACCOUNT_CHECK_SECONDS),
            ),
        )
        check_id = f"AC-{cursor.lastrowid:06d}"
        db.execute("UPDATE account_checks SET id = ? WHERE seq = ?", (check_id, cursor.lastrowid))
        db.commit()
    log.info("account check %s requested for %s (reference %s)", check_id, body["iban"], body.get("reference"))
    return 202, check_view(check_id)


def check_view(check_id):
    row = db.execute("SELECT * FROM account_checks WHERE id = ?", (check_id,)).fetchone()
    if row is None:
        return None
    view = {
        "id": row["id"],
        "reference": row["reference"],
        "iban": row["iban"],
        "name": row["name"],
        "requested_at": row["created_at"],
        "last_polled_at": row["polled_at"],
    }
    if time.time() >= row["ready_at"]:
        view.update(status="complete", result=row["result"], account_holder=row["holder"])
    else:
        view.update(status="pending", expected_at=stamp(datetime.fromtimestamp(row["ready_at"], UTC)))
    return view


def get_check(check_id, query):
    row = db.execute("SELECT ready_at FROM account_checks WHERE id = ?", (check_id,)).fetchone()
    if row is None:
        return 404, {"error": "not found"}
    wait = min(float(query.get("wait", ["0"])[0] or 0), 60.0)
    with lock:
        db.execute("UPDATE account_checks SET polled_at = ? WHERE id = ?", (stamp(), check_id))
        db.commit()
    remaining = row["ready_at"] - time.time()
    if 0 < remaining <= wait:
        time.sleep(remaining)
    elif remaining > 0 and wait:
        time.sleep(wait)
    view = check_view(check_id)
    if view["status"] == "complete":
        with lock:
            db.execute("UPDATE account_checks SET served_at = COALESCE(served_at, ?) WHERE id = ?", (stamp(), check_id))
            db.commit()
    return 200, view


def list_checks(query):
    limit = int(query.get("limit", ["200"])[0])
    ids = [r["id"] for r in db.execute("SELECT id FROM account_checks ORDER BY seq DESC LIMIT ?", (limit,))]
    return 200, [check_view(i) for i in ids]


def cancel(payout_id, body):
    with lock:
        row = payout(payout_id)
        if row is None:
            return 404, {"error": f"unknown payout {payout_id}"}
        if row["status"] != "scheduled":
            return 409, {"error": f"payout {payout_id} is {row['status']}"}
        reason = body.get("reason") or ""
        db.execute(
            "UPDATE payouts SET status = 'cancelled', cancelled_at = ?, cancel_reason = ? WHERE id = ?",
            (stamp(), reason, payout_id),
        )
        db.commit()
        row = payout(payout_id)
    log.info("payout %s cancelled (reference %s): %s", payout_id, row["reference"], reason)
    return 200, public(row)


def listing(query):
    sql, args, clauses = "SELECT * FROM payouts", [], []
    for key in ("status", "reference", "batch_id"):
        if query.get(key):
            clauses.append(f"{key} = ?")
            args.append(query[key][0])
    if query.get("since"):
        clauses.append("created_at >= ?")
        args.append(query["since"][0])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(int(query.get("limit", ["1000"])[0]))
    return 200, [public(r) for r in db.execute(sql, args)]


def settle_loop():
    while True:
        time.sleep(30)
        with lock:
            due = [
                r["id"]
                for r in db.execute("SELECT id FROM payouts WHERE status = 'scheduled' AND settles_at <= ?", (stamp(),))
            ]
            for payout_id in due:
                db.execute("UPDATE payouts SET status = 'settled', settled_at = ? WHERE id = ?", (stamp(), payout_id))
            db.commit()
        for payout_id in due:
            log.info("payout %s settled", payout_id)


ROUTES = [
    ("POST", re.compile(r"^/v1/payouts$"), lambda m, q, b, h: create(b, h.get("Idempotency-Key"))),
    ("POST", re.compile(r"^/v1/batches$"), lambda m, q, b: create_batch(b)),
    ("GET", re.compile(r"^/v1/batches$"), lambda m, q, b: list_batches(q)),
    (
        "GET",
        re.compile(r"^/v1/batches/([^/]+)$"),
        lambda m, q, b: (200, r) if (r := get_batch(m.group(1))) else (404, {"error": "not found"}),
    ),
    ("POST", re.compile(r"^/v1/batches/([^/]+)/status$"), lambda m, q, b: report_batch(m.group(1), b)),
    ("POST", re.compile(r"^/v1/accounts$"), lambda m, q, b: register_account(b)),
    ("POST", re.compile(r"^/v1/account-checks$"), lambda m, q, b: create_check(b)),
    ("GET", re.compile(r"^/v1/account-checks$"), lambda m, q, b: list_checks(q)),
    ("GET", re.compile(r"^/v1/account-checks/([^/]+)$"), lambda m, q, b: get_check(m.group(1), q)),
    ("POST", re.compile(r"^/v1/payouts/([^/]+)/cancel$"), lambda m, q, b: cancel(m.group(1), b)),
    ("GET", re.compile(r"^/v1/payouts$"), lambda m, q, b: listing(q)),
    (
        "GET",
        re.compile(r"^/v1/payouts/([^/]+)$"),
        lambda m, q, b: (200, public(r)) if (r := payout(m.group(1))) else (404, {"error": "not found"}),
    ),
    ("GET", re.compile(r"^/healthz$"), lambda m, q, b: (200, {"status": "ok"})),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle_any(self, method):
        url = urlsplit(self.path)
        body = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
            except ValueError:
                body = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        for route_method, pattern, handler in ROUTES:
            match = pattern.match(url.path)
            if match and route_method == method:
                try:
                    if handler.__code__.co_argcount == 4:
                        status, payload = handler(match, parse_qs(url.query), body, self.headers)
                    else:
                        status, payload = handler(match, parse_qs(url.query), body)
                except Exception as error:
                    log.exception("%s %s failed", method, url.path)
                    status, payload = 500, {"error": str(error)}
                break
        else:
            status, payload = 404, {"error": "not found"}
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


if __name__ == "__main__":
    threading.Thread(target=settle_loop, daemon=True).start()
    log.info("payments gateway listening on :%d", PORT)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
