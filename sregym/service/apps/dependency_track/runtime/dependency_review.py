"""AppSec dependency review queue for Dependency-Track.

Receives Dependency-Track's NEW_VULNERABLE_DEPENDENCY webhook notifications
(POST /hooks/dependency-track, signed with HMAC-SHA256 in X-Webhook-Signature)
and opens one review item per vulnerable dependency a project version
introduces. Every AUDIT_INTERVAL seconds it compares the portfolio's findings
with the review items and reports vulnerable dependencies that have none once
they are older than AUDIT_GRACE seconds.

HTTP: GET /reviews?project=<uuid>, GET /audit, GET /metrics, GET /healthz.
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
AUDIT_GRACE = int(os.environ.get("AUDIT_GRACE_SECONDS", "900"))
DB_PATH = os.environ.get("DB_PATH", "/var/lib/review/reviews.db")
PORT = int(os.environ.get("PORT", "8080"))
GROUP = "GROUP_NEW_VULNERABLE_DEPENDENCY"

lock = threading.Lock()
counters = {"webhooks_ok": 0, "webhooks_rejected": 0, "webhooks_ignored": 0, "reviews_opened": 0, "duplicates": 0}
audit_state = {"last_run": 0.0, "findings": 0, "unreviewed": {}, "errors": 0}
REVIEW_KEYS = (
    "id",
    "project_uuid",
    "project",
    "version",
    "component_uuid",
    "purl",
    "vulnerability",
    "severity",
    "notification_id",
    "sender",
    "opened_at",
)


def log(level, msg, **fields):
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "msg": msg, **fields}
    print(json.dumps(record), flush=True)


def database():
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.executescript(
        """CREATE TABLE IF NOT EXISTS reviews (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             project_uuid TEXT, project TEXT, version TEXT,
             component_uuid TEXT, purl TEXT, vulnerability TEXT, severity TEXT,
             notification_id TEXT, sender TEXT, opened_at REAL,
             UNIQUE (project_uuid, component_uuid, vulnerability));
           CREATE TABLE IF NOT EXISTS seen (
             project_uuid TEXT, component_uuid TEXT, vulnerability TEXT, first_seen REAL,
             PRIMARY KEY (project_uuid, component_uuid, vulnerability));"""
    )
    connection.commit()
    return connection


db = database()


def vulnerability_id(vulnerability):
    return vulnerability.get("vulnId") or vulnerability.get("vuln_id") or vulnerability.get("uuid")


def open_reviews(notification, sender):
    subject = notification.get("subject") or {}
    project = subject.get("project") or {}
    component = subject.get("component") or {}
    opened = []
    with lock:
        for vulnerability in subject.get("vulnerabilities") or []:
            cursor = db.execute(
                """INSERT OR IGNORE INTO reviews (project_uuid, project, version, component_uuid, purl,
                     vulnerability, severity, notification_id, sender, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project.get("uuid"),
                    project.get("name"),
                    project.get("version"),
                    component.get("uuid"),
                    component.get("purl"),
                    vulnerability_id(vulnerability),
                    vulnerability.get("severity"),
                    notification.get("id"),
                    sender,
                    time.time(),
                ),
            )
            if cursor.rowcount == 0:
                counters["duplicates"] += 1
            else:
                counters["reviews_opened"] += 1
                opened.append((cursor.lastrowid, vulnerability_id(vulnerability)))
        db.commit()
    return opened


def dtrack(path):
    request = urllib.request.Request(
        DTRACK_URL + path,
        headers={"X-Api-Key": API_KEY, "Accept": "application/json", "User-Agent": "appsec-review/0.9"},
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
        reviewed = {tuple(row) for row in db.execute("SELECT project_uuid, component_uuid, vulnerability FROM reviews")}
    unreviewed, total = {}, 0
    for project in paged("/api/v1/project?excludeInactive=true"):
        _, findings = dtrack(f"/api/v1/finding/project/{project['uuid']}")
        for finding in findings or []:
            key = (project["uuid"], finding["component"]["uuid"], finding["vulnerability"]["vulnId"])
            total += 1
            with lock:
                db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?)", (*key, started))
                first_seen = db.execute(
                    "SELECT first_seen FROM seen WHERE project_uuid = ? AND component_uuid = ? AND vulnerability = ?",
                    key,
                ).fetchone()[0]
            if key in reviewed or first_seen > started - AUDIT_GRACE:
                continue
            name = (project["uuid"], project["name"], project.get("version", ""))
            unreviewed[name] = unreviewed.get(name, 0) + 1
    with lock:
        db.commit()
        audit_state.update(last_run=time.time(), findings=total, unreviewed=unreviewed)
    for (project_uuid, name, version), count in sorted(unreviewed.items()):
        log(
            "warning",
            "vulnerable dependencies without review",
            project=name,
            version=version,
            project_uuid=project_uuid,
            unreviewed=count,
        )
    log(
        "info",
        "audit finished",
        findings=total,
        reviews=len(reviewed),
        unreviewed=sum(unreviewed.values()),
        seconds=round(time.time() - started, 1),
    )


def auditor():
    time.sleep(60)
    while True:
        try:
            audit()
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
            with lock:
                audit_state["errors"] += 1
            log("error", "audit failed", error=str(error)[:300])
        time.sleep(AUDIT_INTERVAL)


def metrics():
    with lock:
        lines = [
            "# TYPE review_webhooks_total counter",
            f'review_webhooks_total{{result="accepted"}} {counters["webhooks_ok"]}',
            f'review_webhooks_total{{result="rejected"}} {counters["webhooks_rejected"]}',
            f'review_webhooks_total{{result="ignored"}} {counters["webhooks_ignored"]}',
            "# TYPE review_items_opened_total counter",
            f"review_items_opened_total {counters['reviews_opened']}",
            "# TYPE review_duplicate_notifications_total counter",
            f"review_duplicate_notifications_total {counters['duplicates']}",
            "# TYPE review_audit_last_run_timestamp_seconds gauge",
            f"review_audit_last_run_timestamp_seconds {audit_state['last_run']:.0f}",
            "# TYPE review_audit_findings gauge",
            f"review_audit_findings {audit_state['findings']}",
            "# TYPE review_audit_errors_total counter",
            f"review_audit_errors_total {audit_state['errors']}",
            "# TYPE review_audit_unreviewed_dependencies_total gauge",
            f"review_audit_unreviewed_dependencies_total {sum(audit_state['unreviewed'].values())}",
            "# TYPE review_audit_unreviewed_dependencies gauge",
        ]
        for (_, name, version), count in sorted(audit_state["unreviewed"].items()):
            lines.append(f'review_audit_unreviewed_dependencies{{project="{name}",version="{version}"}} {count}')
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
        if notification.get("group") != GROUP:
            with lock:
                counters["webhooks_ignored"] += 1
            self.reply(202, {"reviews": []})
            return
        with lock:
            counters["webhooks_ok"] += 1
        opened = open_reviews(notification, self.client_address[0])
        subject = notification.get("subject") or {}
        for review, vulnerability in opened:
            log(
                "info",
                "review opened",
                review=f"DEP-{review}",
                project=(subject.get("project") or {}).get("name"),
                version=(subject.get("project") or {}).get("version"),
                component=(subject.get("component") or {}).get("purl"),
                vulnerability=vulnerability,
            )
        self.reply(201 if opened else 200, {"reviews": [f"DEP-{review}" for review, _ in opened]})

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
        elif url.path == "/reviews":
            query = urllib.parse.parse_qs(url.query)
            sql = f"SELECT {', '.join(REVIEW_KEYS)} FROM reviews"
            args = []
            if "project" in query:
                sql += " WHERE project_uuid = ?"
                args.append(query["project"][0])
            with lock:
                rows = db.execute(sql + " ORDER BY id", args).fetchall()
            self.reply(200, [dict(zip(REVIEW_KEYS, row, strict=True)) for row in rows])
        elif url.path == "/audit":
            with lock:
                state = {
                    "last_run": audit_state["last_run"],
                    "findings": audit_state["findings"],
                    "unreviewed": [
                        {"project_uuid": key[0], "project": key[1], "version": key[2], "unreviewed": count}
                        for key, count in sorted(audit_state["unreviewed"].items())
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
    log("info", "review queue started", dtrack=DTRACK_URL, audit_interval=AUDIT_INTERVAL)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
