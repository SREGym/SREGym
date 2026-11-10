"""Security ticket bridge for Dependency-Track.

Receives Dependency-Track webhook notifications (POST /hooks/dependency-track,
signed with HMAC-SHA256 in X-Webhook-Signature) and opens one ticket per policy
violation. Every AUDIT_INTERVAL seconds it compares the portfolio's policy
violations with the open tickets and reports violations that have no ticket.

HTTP: GET /tickets?project=<uuid>, GET /audit, GET /metrics, GET /healthz.
"""

import hashlib
import hmac
import http.server
import json
import os
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DTRACK_URL = os.environ.get("DTRACK_URL", "http://dt-api-server:8080").rstrip("/")
API_KEY = os.environ.get("DTRACK_API_KEY", "")
SIGNING_SECRET = os.environ.get("SIGNING_SECRET", "").encode()
AUDIT_INTERVAL = int(os.environ.get("AUDIT_INTERVAL", "300"))
AUDIT_GRACE = int(os.environ.get("AUDIT_GRACE_SECONDS", "600"))
DB_PATH = os.environ.get("DB_PATH", "/var/lib/tracker/tickets.db")
PORT = int(os.environ.get("PORT", "8080"))

lock = threading.Lock()
counters = {"webhooks_ok": 0, "webhooks_rejected": 0, "webhooks_ignored": 0, "tickets_opened": 0, "duplicates": 0}
audit_state = {"last_run": 0.0, "last_duration": 0.0, "violations": 0, "untracked": {}, "errors": 0}


def log(level, msg, **fields):
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "msg": msg, **fields}
    print(json.dumps(record), flush=True)


def database():
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS tickets (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             violation_uuid TEXT UNIQUE NOT NULL,
             project_uuid TEXT, project TEXT, version TEXT,
             component_uuid TEXT, purl TEXT,
             policy TEXT, condition_uuid TEXT, violation_type TEXT,
             notification_id TEXT, sender TEXT, opened_at REAL)"""
    )
    connection.commit()
    return connection


db = database()


def open_ticket(notification, sender):
    subject = notification.get("subject") or {}
    violation = subject.get("policyViolation") or {}
    project = subject.get("project") or {}
    component = subject.get("component") or {}
    condition = violation.get("condition") or {}
    if not violation.get("uuid"):
        return None
    with lock:
        cursor = db.execute(
            """INSERT OR IGNORE INTO tickets (violation_uuid, project_uuid, project, version, component_uuid, purl,
                 policy, condition_uuid, violation_type, notification_id, sender, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                violation["uuid"],
                project.get("uuid"),
                project.get("name"),
                project.get("version"),
                component.get("uuid"),
                component.get("purl"),
                (condition.get("policy") or {}).get("name"),
                condition.get("uuid"),
                violation.get("type"),
                notification.get("id"),
                sender,
                time.time(),
            ),
        )
        db.commit()
        if cursor.rowcount == 0:
            counters["duplicates"] += 1
            return None
        counters["tickets_opened"] += 1
        return cursor.lastrowid


def dtrack(path):
    request = urllib.request.Request(
        DTRACK_URL + path, headers={"X-Api-Key": API_KEY, "Accept": "application/json", "User-Agent": "sec-tracker/1.4"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.headers, json.loads(response.read().decode() or "null")


def paged(path):
    items, page = [], 1
    separator = "&" if "?" in path else "?"
    while True:
        headers, batch = dtrack(f"{path}{separator}pageSize=500&pageNumber={page}")
        items.extend(batch or [])
        if not batch or len(items) >= int(headers.get("X-Total-Count", "0")):
            return items
        page += 1


def audit():
    started = time.time()
    with lock:
        tracked = {row[0] for row in db.execute("SELECT violation_uuid FROM tickets")}
    untracked, total = {}, 0
    for project in paged("/api/v1/project?excludeInactive=true"):
        for violation in paged(f"/api/v1/violation/project/{project['uuid']}"):
            total += 1
            if violation["uuid"] in tracked or violation.get("timestamp", 0) / 1000 > started - AUDIT_GRACE:
                continue
            key = (project["uuid"], project["name"], project.get("version", ""))
            untracked[key] = untracked.get(key, 0) + 1
    with lock:
        audit_state.update(
            last_run=time.time(), last_duration=time.time() - started, violations=total, untracked=untracked
        )
    for (project_uuid, name, version), count in sorted(untracked.items()):
        log(
            "warning",
            "policy violations without ticket",
            project=name,
            version=version,
            project_uuid=project_uuid,
            untracked=count,
        )
    log(
        "info",
        "audit finished",
        violations=total,
        tickets=len(tracked),
        untracked=sum(untracked.values()),
        seconds=round(time.time() - started, 1),
    )


def auditor():
    time.sleep(60)
    while True:
        try:
            audit()
        except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
            with lock:
                audit_state["errors"] += 1
            log("error", "audit failed", error=str(error)[:300])
        time.sleep(AUDIT_INTERVAL)


def metrics():
    with lock:
        lines = [
            "# TYPE tracker_webhooks_total counter",
            f'tracker_webhooks_total{{result="accepted"}} {counters["webhooks_ok"]}',
            f'tracker_webhooks_total{{result="rejected"}} {counters["webhooks_rejected"]}',
            f'tracker_webhooks_total{{result="ignored"}} {counters["webhooks_ignored"]}',
            "# TYPE tracker_tickets_opened_total counter",
            f"tracker_tickets_opened_total {counters['tickets_opened']}",
            "# TYPE tracker_duplicate_notifications_total counter",
            f"tracker_duplicate_notifications_total {counters['duplicates']}",
            "# TYPE tracker_audit_last_run_timestamp_seconds gauge",
            f"tracker_audit_last_run_timestamp_seconds {audit_state['last_run']:.0f}",
            "# TYPE tracker_audit_violations gauge",
            f"tracker_audit_violations {audit_state['violations']}",
            "# TYPE tracker_audit_errors_total counter",
            f"tracker_audit_errors_total {audit_state['errors']}",
            "# TYPE tracker_audit_untracked_violations_total gauge",
            f"tracker_audit_untracked_violations_total {sum(audit_state['untracked'].values())}",
            "# TYPE tracker_audit_untracked_violations gauge",
        ]
        for (_, name, version), count in sorted(audit_state["untracked"].items()):
            lines.append(f'tracker_audit_untracked_violations{{project="{name}",version="{version}"}} {count}')
    return "\n".join(lines) + "\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/hooks/dependency-track":
            self.reply(404, {"error": "not found"})
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        expected = "sha256=" + hmac.new(SIGNING_SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.headers.get("X-Webhook-Signature", "")):
            with lock:
                counters["webhooks_rejected"] += 1
            log("warning", "rejected webhook with invalid signature", sender=self.client_address[0])
            self.reply(401, {"error": "invalid signature"})
            return
        try:
            notification = json.loads(body).get("notification") or {}
        except ValueError:
            self.reply(400, {"error": "invalid json"})
            return
        if notification.get("group") != "GROUP_POLICY_VIOLATION":
            with lock:
                counters["webhooks_ignored"] += 1
            self.reply(202, {"ticket": None})
            return
        with lock:
            counters["webhooks_ok"] += 1
        ticket = open_ticket(notification, self.client_address[0])
        if ticket:
            subject = notification.get("subject") or {}
            log(
                "info",
                "ticket opened",
                ticket=f"SEC-{ticket}",
                project=(subject.get("project") or {}).get("name"),
                version=(subject.get("project") or {}).get("version"),
                component=(subject.get("component") or {}).get("purl"),
                policy=(((subject.get("policyViolation") or {}).get("condition") or {}).get("policy") or {}).get(
                    "name"
                ),
            )
        self.reply(201 if ticket else 200, {"ticket": f"SEC-{ticket}" if ticket else None})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/healthz":
            self.reply(200, {"status": "ok"})
        elif url.path == "/metrics":
            data = metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif url.path == "/tickets":
            query = urllib.parse.parse_qs(url.query)
            sql = (
                "SELECT id, violation_uuid, project_uuid, project, version, component_uuid, purl, policy, "
                "condition_uuid, violation_type, notification_id, sender, opened_at FROM tickets"
            )
            args = []
            if "project" in query:
                sql += " WHERE project_uuid = ?"
                args.append(query["project"][0])
            with lock:
                rows = db.execute(sql + " ORDER BY id", args).fetchall()
            keys = (
                "id",
                "violation_uuid",
                "project_uuid",
                "project",
                "version",
                "component_uuid",
                "purl",
                "policy",
                "condition_uuid",
                "violation_type",
                "notification_id",
                "sender",
                "opened_at",
            )
            self.reply(200, [dict(zip(keys, row, strict=True)) for row in rows])
        elif url.path == "/audit":
            with lock:
                state = {
                    "last_run": audit_state["last_run"],
                    "violations": audit_state["violations"],
                    "untracked": [
                        {"project_uuid": key[0], "project": key[1], "version": key[2], "untracked": count}
                        for key, count in sorted(audit_state["untracked"].items())
                    ],
                }
            self.reply(200, state)
        else:
            self.reply(404, {"error": "not found"})

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    threading.Thread(target=auditor, daemon=True).start()
    log("info", "tracker started", dtrack=DTRACK_URL, audit_interval=AUDIT_INTERVAL)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
