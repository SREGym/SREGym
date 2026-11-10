import json
import logging
import os
import re
import sqlite3
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DB = os.environ.get("PORTAL_DB", "/data/portal.db")
PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("portal")
lock = threading.Lock()


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect():
    db = sqlite3.connect(DB, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, vendor TEXT, iban TEXT, amount TEXT, currency TEXT,
            requester TEXT, manager TEXT, status TEXT, approved_by TEXT, released_by TEXT,
            payout_id TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, stage TEXT, assignee TEXT,
            link TEXT, state TEXT, created_at TEXT, expires_at TEXT, closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, at TEXT, event TEXT, detail TEXT
        );
        CREATE INDEX IF NOT EXISTS tasks_request ON tasks(request_id);
        CREATE INDEX IF NOT EXISTS history_request ON history(request_id);
        CREATE TABLE IF NOT EXISTS invoices (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, vendor TEXT, iban TEXT, amount TEXT,
            currency TEXT, approved_by TEXT, approved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payment_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, batch_id TEXT, invoices INTEGER, total TEXT,
            outcome TEXT, at TEXT
        );
        CREATE TABLE IF NOT EXISTS vendors (
            id TEXT PRIMARY KEY, name TEXT, iban TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS bank_changes (
            id TEXT PRIMARY KEY, vendor_id TEXT, new_iban TEXT, account_name TEXT, requested_by TEXT,
            status TEXT, verification TEXT, check_id TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, vendor TEXT, iban TEXT, amount TEXT, currency TEXT, status TEXT,
            delivery_link TEXT, invoice_link TEXT, payout_id TEXT, received_by TEXT, invoice_no TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS object_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, object_id TEXT, at TEXT, event TEXT, detail TEXT
        );
        CREATE INDEX IF NOT EXISTS object_history_object ON object_history(kind, object_id);
        CREATE TABLE IF NOT EXISTS payment_batches (
            id TEXT PRIMARY KEY, lines INTEGER, total TEXT, status TEXT, cutoff_at TEXT,
            created_at TEXT, settled_at TEXT, overdue INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS remittances (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, line_id TEXT, vendor TEXT, amount TEXT,
            currency TEXT, payout_id TEXT, advice TEXT, created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS remittances_batch ON remittances(batch_id);
        """
    )
    return db


db = connect()
for statement in (
    "ALTER TABLE invoices ADD COLUMN status TEXT DEFAULT 'approved'",
    "ALTER TABLE invoices ADD COLUMN payout_id TEXT",
    "ALTER TABLE invoices ADD COLUMN paid_at TEXT",
):
    try:  # noqa: SIM105
        db.execute(statement)
    except sqlite3.OperationalError:
        pass


def stage_link(resume_url, stage):
    base, sep, query = resume_url.partition("?")
    return f"{base.rstrip('/')}/{stage}{sep}{query}"


def record(request_id, event, detail=""):
    db.execute(
        "INSERT INTO history (request_id, at, event, detail) VALUES (?, ?, ?, ?)",
        (request_id, now(), event, detail),
    )


def get_request(request_id):
    row = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["tasks"] = [
        dict(t) for t in db.execute("SELECT * FROM tasks WHERE request_id = ? ORDER BY id", (request_id,))
    ]
    result["history"] = [
        dict(h)
        for h in db.execute("SELECT at, event, detail FROM history WHERE request_id = ? ORDER BY id", (request_id,))
    ]
    return result


def close_tasks(request_id):
    db.execute(
        "UPDATE tasks SET state = 'closed', closed_at = ? WHERE request_id = ? AND state = 'open'",
        (now(), request_id),
    )


def create_request(body):
    request_id = str(body.get("id") or "").strip()
    if not request_id:
        return 400, {"error": "id is required"}
    stamp = now()
    with lock:
        if db.execute("SELECT 1 FROM requests WHERE id = ?", (request_id,)).fetchone():
            return 409, {"error": f"request {request_id} exists"}
        db.execute(
            "INSERT INTO requests (id, vendor, iban, amount, currency, requester, manager, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?)",
            (
                request_id,
                body.get("vendor"),
                body.get("iban"),
                str(body.get("amount")),
                body.get("currency", "EUR"),
                body.get("requester"),
                body.get("manager"),
                stamp,
                stamp,
            ),
        )
        record(request_id, "submitted", f"{body.get('amount')} {body.get('currency', 'EUR')} to {body.get('vendor')}")
        db.commit()
    log.info(
        "request %s submitted by %s: %s %s to %s",
        request_id,
        body.get("requester"),
        body.get("amount"),
        body.get("currency", "EUR"),
        body.get("vendor"),
    )
    return 201, get_request(request_id)


def create_task(request_id, body):
    stage = body.get("stage")
    if stage not in ("manager", "finance"):
        return 400, {"error": "stage must be manager or finance"}
    resume_url = body.get("resume_url") or ""
    with lock:
        request = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if request is None:
            return 404, {"error": f"unknown request {request_id}"}
        assignee = body.get("assignee") or (request["manager"] if stage == "manager" else "finance")
        stamp = now()
        cursor = db.execute(
            "INSERT INTO tasks (request_id, stage, assignee, link, state, created_at, expires_at) VALUES (?, ?, ?, ?, 'open', ?, ?)",
            (request_id, stage, assignee, stage_link(resume_url, stage), stamp, body.get("expires_at")),
        )
        status = "awaiting_manager" if stage == "manager" else "awaiting_release"
        if stage == "finance":
            db.execute(
                "UPDATE requests SET status = ?, approved_by = ?, updated_at = ? WHERE id = ?",
                (status, body.get("approved_by"), stamp, request_id),
            )
            record(request_id, "manager_approved", body.get("approved_by") or "")
        else:
            db.execute("UPDATE requests SET status = ?, updated_at = ? WHERE id = ?", (status, stamp, request_id))
        record(request_id, f"{stage}_task", assignee)
        db.commit()
        task = dict(db.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone())
    log.info("request %s waiting for %s (%s), task %s", request_id, stage, assignee, task["id"])
    return 201, task


def close_request(request_id, body):
    outcome = body.get("outcome") or "closed"
    with lock:
        if not db.execute("SELECT 1 FROM requests WHERE id = ?", (request_id,)).fetchone():
            return 404, {"error": f"unknown request {request_id}"}
        db.execute("UPDATE requests SET status = ?, updated_at = ? WHERE id = ?", (outcome, now(), request_id))
        close_tasks(request_id)
        record(request_id, outcome, body.get("by") or "")
        db.commit()
    log.info("request %s %s", request_id, outcome)
    return 200, get_request(request_id)


def mark_paid(request_id, body):
    payout_id = body.get("payout_id") or ""
    released_by = (body.get("released_by") or "").strip()
    with lock:
        if not db.execute("SELECT 1 FROM requests WHERE id = ?", (request_id,)).fetchone():
            return 404, {"error": f"unknown request {request_id}"}
        db.execute(
            "UPDATE requests SET status = 'paid', payout_id = ?, released_by = ?, updated_at = ? WHERE id = ?",
            (payout_id, released_by or None, now(), request_id),
        )
        close_tasks(request_id)
        if released_by:
            record(request_id, "released", released_by)
        record(request_id, "paid", payout_id)
        db.commit()
    if released_by:
        log.info("request %s paid, payout %s, released by %s", request_id, payout_id, released_by)
    else:
        log.warning("request %s paid, payout %s, without a finance release", request_id, payout_id)
    return 200, get_request(request_id)


def list_requests(query):
    sql, args = "SELECT * FROM requests", []
    clauses = []
    if query.get("status"):
        clauses.append("status = ?")
        args.append(query["status"][0])
    if query.get("since"):
        clauses.append("created_at >= ?")
        args.append(query["since"][0])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(query.get("limit", ["500"])[0]))
    return 200, [dict(r) for r in db.execute(sql, args)]


def list_tasks(query):
    sql, args = "SELECT * FROM tasks", []
    clauses = []
    for key in ("state", "stage", "request_id"):
        if query.get(key):
            clauses.append(f"{key} = ?")
            args.append(query[key][0])
    if query.get("after"):
        clauses.append("id > ?")
        args.append(int(query["after"][0]))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id LIMIT ?"
    args.append(int(query.get("limit", ["1000"])[0]))
    return 200, [dict(r) for r in db.execute(sql, args)]


def note(kind, object_id, event, detail=""):
    db.execute(
        "INSERT INTO object_history (kind, object_id, at, event, detail) VALUES (?, ?, ?, ?, ?)",
        (kind, object_id, now(), event, detail),
    )


def object_history(kind, object_id):
    return [
        dict(h)
        for h in db.execute(
            "SELECT at, event, detail FROM object_history WHERE kind = ? AND object_id = ? ORDER BY id",
            (kind, object_id),
        )
    ]


# ---------------------------------------------------------------- approved invoices and payment runs
def create_invoice(body):
    invoice_id = str(body.get("id") or "").strip()
    missing = [k for k in ("vendor", "iban", "amount") if not body.get(k)]
    if not invoice_id or missing:
        return 400, {"error": f"missing {', '.join(['id'] if not invoice_id else missing)}"}
    with lock:
        if db.execute("SELECT 1 FROM invoices WHERE id = ?", (invoice_id,)).fetchone():
            return 409, {"error": f"invoice {invoice_id} exists"}
        db.execute(
            "INSERT INTO invoices (id, vendor, iban, amount, currency, approved_by, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                invoice_id,
                body["vendor"],
                body["iban"],
                str(body["amount"]),
                body.get("currency", "EUR"),
                body.get("approved_by"),
                now(),
            ),
        )
        db.commit()
        row = dict(db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone())
    log.info(
        "invoice %s approved for payment by %s: %s %s to %s",
        invoice_id,
        row["approved_by"],
        row["amount"],
        row["currency"],
        row["vendor"],
    )
    return 201, row


def approvals(query):
    after = int(query.get("after", ["0"])[0] or 0)
    limit = int(query.get("limit", ["500"])[0])
    rows = db.execute("SELECT * FROM invoices WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit))
    return 200, [dict(r) for r in rows]


def invoice_paid(invoice_id, body):
    payout_id = str(body.get("payout_id") or "").strip()
    if not payout_id:
        return 400, {"error": "missing payout_id"}
    with lock:
        row = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if row is None:
            return 404, {"error": f"unknown invoice {invoice_id}"}
        earlier = row["payout_id"]
        db.execute(
            "UPDATE invoices SET status = 'paid', payout_id = ?, paid_at = ? WHERE id = ?",
            (payout_id, now(), invoice_id),
        )
        note("invoice", invoice_id, "paid", payout_id)
        db.commit()
        row = dict(db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone())
    if earlier and earlier != payout_id:
        log.warning("invoice %s paid again with payout %s (earlier payout %s)", invoice_id, payout_id, earlier)
    else:
        log.info("invoice %s paid with payout %s", invoice_id, payout_id)
    return 200, row


def get_invoice(invoice_id):
    with lock:
        row = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if row is None:
            return 404, {"error": "not found"}
        result = dict(row)
        result["history"] = [
            dict(h)
            for h in db.execute(
                "SELECT at, event, detail FROM object_history WHERE kind = 'invoice' AND object_id = ? ORDER BY id",
                (invoice_id,),
            )
        ]
    return 200, result


def list_invoices(query):
    sql, args = "SELECT * FROM invoices", []
    if query.get("status"):
        sql += " WHERE status = ?"
        args.append(query["status"][0])
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(int(query.get("limit", ["500"])[0]))
    with lock:
        rows = [dict(r) for r in db.execute(sql, args)]
    return 200, rows


def record_run(body):
    stamp = now()
    with lock:
        db.execute(
            "INSERT INTO payment_runs (run, batch_id, invoices, total, outcome, at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(body.get("run") or ""),
                body.get("batch_id"),
                int(body.get("invoices") or 0),
                str(body.get("total") or ""),
                body.get("outcome") or "",
                stamp,
            ),
        )
        db.commit()
    log.info(
        "payment run %s: batch %s with %s invoices (%s), bank status %s",
        body.get("run"),
        body.get("batch_id"),
        body.get("invoices"),
        body.get("total"),
        body.get("outcome"),
    )
    return 201, {"run": body.get("run"), "at": stamp}


def list_runs(query):
    limit = int(query.get("limit", ["200"])[0])
    return 200, [dict(r) for r in db.execute("SELECT * FROM payment_runs ORDER BY id DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- vendor master and bank-detail changes
def upsert_vendor(body):
    vendor_id = str(body.get("id") or "").strip()
    if not vendor_id or not body.get("name") or not body.get("iban"):
        return 400, {"error": "id, name and iban are required"}
    with lock:
        db.execute(
            "INSERT INTO vendors (id, name, iban, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
            (vendor_id, body["name"], body["iban"], now()),
        )
        db.commit()
        row = dict(db.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone())
    return 200, row


def get_vendor(vendor_id):
    row = db.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "history": object_history("vendor", vendor_id)}


def create_bank_change(body):
    change_id = str(body.get("id") or "").strip()
    vendor_id = str(body.get("vendor_id") or "").strip()
    if not change_id or not vendor_id or not body.get("new_iban"):
        return 400, {"error": "id, vendor_id and new_iban are required"}
    stamp = now()
    with lock:
        if db.execute("SELECT 1 FROM vendors WHERE id = ?", (vendor_id,)).fetchone() is None:
            return 404, {"error": f"unknown vendor {vendor_id}"}
        existing = db.execute("SELECT status FROM bank_changes WHERE id = ?", (change_id,)).fetchone()
        if existing is not None:
            if existing["status"] not in ("verifying", "review"):
                return 409, {"error": f"bank change {change_id} is {existing['status']}"}
            # resubmitting an open change restarts its verification
            db.execute(
                "UPDATE bank_changes SET status = 'verifying', verification = NULL, check_id = NULL, updated_at = ? "
                "WHERE id = ?",
                (stamp, change_id),
            )
            note("bank_change", change_id, "resubmitted", f"by {body.get('requested_by')}")
            db.commit()
            log.info("bank change %s resubmitted by %s", change_id, body.get("requested_by"))
            return 200, get_bank_change(change_id)
        db.execute(
            "INSERT INTO bank_changes (id, vendor_id, new_iban, account_name, requested_by, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'verifying', ?, ?)",
            (change_id, vendor_id, body["new_iban"], body.get("account_name"), body.get("requested_by"), stamp, stamp),
        )
        note("bank_change", change_id, "submitted", f"{vendor_id} -> {body['new_iban']} by {body.get('requested_by')}")
        db.commit()
    log.info(
        "bank change %s for vendor %s submitted by %s: new account %s",
        change_id,
        vendor_id,
        body.get("requested_by"),
        body["new_iban"],
    )
    return 201, get_bank_change(change_id)


def get_bank_change(change_id):
    row = db.execute("SELECT * FROM bank_changes WHERE id = ?", (change_id,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "history": object_history("bank_change", change_id)}


def bank_change_outcome(change_id, body):
    outcome = body.get("outcome")
    if outcome not in ("applied", "rejected", "review"):
        return 400, {"error": "outcome must be applied, rejected or review"}
    verification = str(body.get("verification") or "")
    stamp = now()
    with lock:
        change = db.execute("SELECT * FROM bank_changes WHERE id = ?", (change_id,)).fetchone()
        if change is None:
            return 404, {"error": f"unknown bank change {change_id}"}
        db.execute(
            "UPDATE bank_changes SET status = ?, verification = ?, check_id = COALESCE(?, check_id), updated_at = ? "
            "WHERE id = ?",
            (outcome, verification, body.get("check_id"), stamp, change_id),
        )
        if outcome == "applied":
            old = db.execute("SELECT iban FROM vendors WHERE id = ?", (change["vendor_id"],)).fetchone()
            db.execute(
                "UPDATE vendors SET iban = ?, updated_at = ? WHERE id = ?",
                (change["new_iban"], stamp, change["vendor_id"]),
            )
            note(
                "vendor",
                change["vendor_id"],
                "bank_account_changed",
                f"{old['iban'] if old else ''} -> {change['new_iban']} (change {change_id})",
            )
        note("bank_change", change_id, outcome, f"verification {verification or '-'} by {body.get('by') or 'n8n'}")
        db.commit()
    if outcome == "applied":
        log.info(
            "vendor %s bank account changed to %s (change %s, verification %s)",
            change["vendor_id"],
            change["new_iban"],
            change_id,
            verification,
        )
    elif outcome == "rejected":
        log.info(
            "bank change %s for vendor %s rejected (verification %s)", change_id, change["vendor_id"], verification
        )
    else:
        log.warning(
            "bank change %s for vendor %s needs manual review (verification %s)",
            change_id,
            change["vendor_id"],
            verification or "-",
        )
    return 200, get_bank_change(change_id)


def list_bank_changes(query):
    sql, args, clauses = "SELECT * FROM bank_changes", [], []
    for key in ("status", "vendor_id"):
        if query.get(key):
            clauses.append(f"{key} = ?")
            args.append(query[key][0])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(query.get("limit", ["500"])[0]))
    return 200, [dict(r) for r in db.execute(sql, args)]


def list_vendors(query):
    return 200, [dict(r) for r in db.execute("SELECT * FROM vendors ORDER BY id")]


# ---------------------------------------------------------------- purchase orders paid on delivery
def create_order(body):
    order_id = str(body.get("id") or "").strip()
    missing = [k for k in ("vendor", "iban", "amount") if not body.get(k)]
    if not order_id or missing:
        return 400, {"error": f"missing {', '.join(['id'] if not order_id else missing)}"}
    stamp = now()
    with lock:
        if db.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone():
            return 409, {"error": f"order {order_id} exists"}
        db.execute(
            "INSERT INTO orders (id, vendor, iban, amount, currency, status, delivery_link, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'awaiting_delivery', ?, ?, ?)",
            (
                order_id,
                body["vendor"],
                body["iban"],
                str(body["amount"]),
                body.get("currency", "EUR"),
                stage_link(body.get("resume_url") or "", "delivery"),
                stamp,
                stamp,
            ),
        )
        note("order", order_id, "ordered", f"{body['amount']} {body.get('currency', 'EUR')} to {body['vendor']}")
        db.commit()
    log.info(
        "order %s placed with %s: %s %s, payable on delivery",
        order_id,
        body["vendor"],
        body["amount"],
        body.get("currency", "EUR"),
    )
    return 201, get_order(order_id)


def get_order(order_id):
    row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "history": object_history("order", order_id)}


def order_delivered(order_id, body):
    stamp = now()
    with lock:
        if db.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone() is None:
            return 404, {"error": f"unknown order {order_id}"}
        db.execute(
            "UPDATE orders SET status = 'delivered', payout_id = ?, received_by = ?, invoice_link = ?, updated_at = ? "
            "WHERE id = ?",
            (
                body.get("payout_id"),
                body.get("received_by"),
                stage_link(body.get("resume_url") or "", "invoice") if body.get("resume_url") else None,
                stamp,
                order_id,
            ),
        )
        note(
            "order",
            order_id,
            "delivered",
            f"received by {body.get('received_by') or '-'}, delivery note {body.get('document') or '-'}, "
            f"payout {body.get('payout_id') or '-'}",
        )
        db.commit()
    log.info(
        "order %s delivered (received by %s), payout %s",
        order_id,
        body.get("received_by"),
        body.get("payout_id"),
    )
    return 200, get_order(order_id)


def order_closed(order_id, body):
    stamp = now()
    with lock:
        if db.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone() is None:
            return 404, {"error": f"unknown order {order_id}"}
        db.execute(
            "UPDATE orders SET status = 'closed', invoice_no = ?, updated_at = ? WHERE id = ?",
            (body.get("invoice_no"), stamp, order_id),
        )
        note("order", order_id, "invoiced", f"invoice {body.get('invoice_no') or '-'}")
        db.commit()
    log.info("order %s closed with supplier invoice %s", order_id, body.get("invoice_no"))
    return 200, get_order(order_id)


def list_orders(query):
    sql, args, clauses = "SELECT * FROM orders", [], []
    if query.get("status"):
        clauses.append("status = ?")
        args.append(query["status"][0])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(query.get("limit", ["500"])[0]))
    return 200, [dict(r) for r in db.execute(sql, args)]


def register_batch(body):
    batch_id = str(body.get("batch_id") or "")
    if not batch_id:
        return 400, {"error": "batch_id is required"}
    with lock:
        stamp = now()
        db.execute(
            "INSERT INTO payment_batches (id, lines, total, status, cutoff_at, created_at) VALUES (?, ?, ?, 'scheduled', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cutoff_at = excluded.cutoff_at WHERE status != 'settled'",
            (batch_id, int(body.get("lines") or 0), str(body.get("total") or ""), body.get("cutoff_at"), stamp),
        )
        note("payment_batch", batch_id, "scheduled", str(body.get("cutoff_at") or ""))
        db.commit()
        row = dict(db.execute("SELECT * FROM payment_batches WHERE id = ?", (batch_id,)).fetchone())
    log.info("payment batch %s scheduled for the bank cut-off %s (%s lines)", batch_id, row["cutoff_at"], row["lines"])
    return 201, row


def settle_batch(batch_id, body):
    with lock:
        row = db.execute("SELECT * FROM payment_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return 404, {"error": f"unknown payment batch {batch_id}"}
        stamp = now()
        db.execute("UPDATE payment_batches SET status = 'settled', settled_at = ? WHERE id = ?", (stamp, batch_id))
        note("payment_batch", batch_id, "settled", str(body.get("lines") or ""))
        db.commit()
        row = dict(db.execute("SELECT * FROM payment_batches WHERE id = ?", (batch_id,)).fetchone())
    log.info("payment batch %s settled", batch_id)
    return 200, row


def add_remittance(body):
    missing = [k for k in ("batch_id", "line_id", "payout_id") if not body.get(k)]
    if missing:
        return 400, {"error": f"missing {', '.join(missing)}"}
    with lock:
        cursor = db.execute(
            "INSERT INTO remittances (batch_id, line_id, vendor, amount, currency, payout_id, advice, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(
                str(body.get(k) or "")
                for k in ("batch_id", "line_id", "vendor", "amount", "currency", "payout_id", "advice")
            )
            + (now(),),
        )
        db.commit()
        row = dict(db.execute("SELECT * FROM remittances WHERE id = ?", (cursor.lastrowid,)).fetchone())
    log.info(
        "remittance advice for %s (%s %s to %s) sent", row["line_id"], row["amount"], row["currency"], row["vendor"]
    )
    return 201, row


def list_batches(query):
    sql, args = "SELECT * FROM payment_batches", []
    if query.get("status"):
        sql += " WHERE status = ?"
        args.append(query["status"][0])
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(query.get("limit", ["500"])[0]))
    with lock:
        return 200, [dict(r) for r in db.execute(sql, args)]


def get_batch(batch_id):
    with lock:
        row = db.execute("SELECT * FROM payment_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return 404, {"error": "not found"}
        result = dict(row)
        result["remittances"] = [
            dict(r) for r in db.execute("SELECT * FROM remittances WHERE batch_id = ? ORDER BY id", (batch_id,))
        ]
        result["history"] = object_history("payment_batch", batch_id)
    return 200, result


def list_remittances(query):
    sql, args = "SELECT * FROM remittances", []
    if query.get("batch_id"):
        sql += " WHERE batch_id = ?"
        args.append(query["batch_id"][0])
    sql += " ORDER BY id LIMIT ?"
    args.append(int(query.get("limit", ["5000"])[0]))
    with lock:
        return 200, [dict(r) for r in db.execute(sql, args)]


OVERDUE_GRACE_S = 300


def watch_batches():
    """Treasury's control: a scheduled batch must be settled shortly after its bank cut-off."""
    stop = threading.Event()
    while not stop.wait(30):
        try:
            with lock:
                rows = [
                    dict(r)
                    for r in db.execute("SELECT * FROM payment_batches WHERE status = 'scheduled' AND overdue = 0")
                ]
                late = []
                for row in rows:
                    try:
                        cutoff = datetime.fromisoformat(str(row["cutoff_at"]).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if (datetime.now(UTC) - cutoff).total_seconds() > OVERDUE_GRACE_S:
                        sent = db.execute(
                            "SELECT count(*) FROM remittances WHERE batch_id = ?", (row["id"],)
                        ).fetchone()[0]
                        late.append((row, sent))
                        db.execute("UPDATE payment_batches SET overdue = 1 WHERE id = ?", (row["id"],))
                db.commit()
            for row, sent in late:
                log.warning(
                    "payment batch %s missed its bank cut-off %s: %d of %d remittance advices sent, batch not settled",
                    row["id"],
                    row["cutoff_at"],
                    sent,
                    row["lines"],
                )
        except Exception:
            log.exception("batch watch failed")


ROUTES = [
    ("POST", re.compile(r"^/api/payment-batches$"), lambda m, q, b: register_batch(b)),
    ("POST", re.compile(r"^/api/payment-batches/([^/]+)/settled$"), lambda m, q, b: settle_batch(m.group(1), b)),
    ("GET", re.compile(r"^/api/payment-batches$"), lambda m, q, b: list_batches(q)),
    ("GET", re.compile(r"^/api/payment-batches/([^/]+)$"), lambda m, q, b: get_batch(m.group(1))),
    ("POST", re.compile(r"^/api/remittances$"), lambda m, q, b: add_remittance(b)),
    ("GET", re.compile(r"^/api/remittances$"), lambda m, q, b: list_remittances(q)),
    ("POST", re.compile(r"^/api/requests$"), lambda m, q, b: create_request(b)),
    ("POST", re.compile(r"^/api/requests/([^/]+)/tasks$"), lambda m, q, b: create_task(m.group(1), b)),
    ("POST", re.compile(r"^/api/requests/([^/]+)/close$"), lambda m, q, b: close_request(m.group(1), b)),
    ("POST", re.compile(r"^/api/requests/([^/]+)/paid$"), lambda m, q, b: mark_paid(m.group(1), b)),
    ("GET", re.compile(r"^/api/requests$"), lambda m, q, b: list_requests(q)),
    (
        "GET",
        re.compile(r"^/api/requests/([^/]+)$"),
        lambda m, q, b: (200, r) if (r := get_request(m.group(1))) else (404, {"error": "not found"}),
    ),
    ("GET", re.compile(r"^/api/tasks$"), lambda m, q, b: list_tasks(q)),
    ("POST", re.compile(r"^/api/invoices$"), lambda m, q, b: create_invoice(b)),
    ("GET", re.compile(r"^/api/invoices$"), lambda m, q, b: list_invoices(q)),
    ("GET", re.compile(r"^/api/invoices/([^/]+)$"), lambda m, q, b: get_invoice(m.group(1))),
    ("POST", re.compile(r"^/api/invoices/([^/]+)/paid$"), lambda m, q, b: invoice_paid(m.group(1), b)),
    ("GET", re.compile(r"^/api/approvals$"), lambda m, q, b: approvals(q)),
    ("POST", re.compile(r"^/api/payment-runs$"), lambda m, q, b: record_run(b)),
    ("GET", re.compile(r"^/api/payment-runs$"), lambda m, q, b: list_runs(q)),
    ("POST", re.compile(r"^/api/vendors$"), lambda m, q, b: upsert_vendor(b)),
    ("GET", re.compile(r"^/api/vendors$"), lambda m, q, b: list_vendors(q)),
    (
        "GET",
        re.compile(r"^/api/vendors/([^/]+)$"),
        lambda m, q, b: (200, r) if (r := get_vendor(m.group(1))) else (404, {"error": "not found"}),
    ),
    ("POST", re.compile(r"^/api/bank-changes$"), lambda m, q, b: create_bank_change(b)),
    ("POST", re.compile(r"^/api/bank-changes/([^/]+)/outcome$"), lambda m, q, b: bank_change_outcome(m.group(1), b)),
    ("GET", re.compile(r"^/api/bank-changes$"), lambda m, q, b: list_bank_changes(q)),
    (
        "GET",
        re.compile(r"^/api/bank-changes/([^/]+)$"),
        lambda m, q, b: (200, r) if (r := get_bank_change(m.group(1))) else (404, {"error": "not found"}),
    ),
    ("POST", re.compile(r"^/api/orders$"), lambda m, q, b: create_order(b)),
    ("POST", re.compile(r"^/api/orders/([^/]+)/delivered$"), lambda m, q, b: order_delivered(m.group(1), b)),
    ("POST", re.compile(r"^/api/orders/([^/]+)/closed$"), lambda m, q, b: order_closed(m.group(1), b)),
    ("GET", re.compile(r"^/api/orders$"), lambda m, q, b: list_orders(q)),
    (
        "GET",
        re.compile(r"^/api/orders/([^/]+)$"),
        lambda m, q, b: (200, r) if (r := get_order(m.group(1))) else (404, {"error": "not found"}),
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
    log.info("payables portal listening on :%d", PORT)
    threading.Thread(target=watch_batches, name="batch-watch", daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
