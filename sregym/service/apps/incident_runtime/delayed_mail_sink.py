"""An eventually consistent audit over the same durable SMTP acceptance ledger.

Publication lag affects only incident mail sent during the ongoing impairment.
The public completeness watermark makes safe reconciliation possible without
assuming synchronized client clocks. Acceptance and its publication schedule
commit atomically and survive process restart.
"""

import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

try:
    from .ambiguous_mail_sink import ambiguous_smtp_handler
    from .intermittent_mail_sink import IntermittentMailbox
    from .mail_sink import SMTPServer
except ImportError:
    from ambiguous_mail_sink import ambiguous_smtp_handler
    from intermittent_mail_sink import IntermittentMailbox
    from mail_sink import SMTPServer


class DelayedAuditMailbox(IntermittentMailbox):
    def __init__(self, path, delay=30.0, clock=None):
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("Invalid publication delay")
        super().__init__(path)
        self.clock = clock
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS audit_policy (id INTEGER PRIMARY KEY, delay REAL NOT NULL)")
            db.execute("INSERT OR IGNORE INTO audit_policy VALUES (1,?)", (delay,))
            self.delay = db.execute("SELECT delay FROM audit_policy WHERE id=1").fetchone()[0]
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit_visibility "
                "(delivery_id INTEGER PRIMARY KEY, accepted_at REAL NOT NULL, visible_at REAL NOT NULL)"
            )
            # Existing entries predate this provider's initialization. Never
            # overwrite timestamps on restart or delay previously visible mail.
            db.execute("INSERT OR IGNORE INTO audit_visibility SELECT id,0,0 FROM deliveries")
            db.execute("""
CREATE TRIGGER IF NOT EXISTS schedule_audit_publication AFTER INSERT ON deliveries
BEGIN
  INSERT INTO audit_visibility(delivery_id,accepted_at,visible_at)
  VALUES (
    NEW.id,
    (julianday('now') - 2440587.5) * 86400.0,
    (julianday('now') - 2440587.5) * 86400.0 + CASE
      WHEN (SELECT active FROM ongoing_fault WHERE id=1) = 1
        AND json_extract(NEW.data, '$.headers."x-gitlab-notificationreason"') = 'sregym-ack'
      THEN (SELECT delay FROM audit_policy WHERE id=1) ELSE 0 END
  );
END
""")

    def page(self, after=0, through=None, observed_at=None, limit=40):
        # Use the same clock and precision as the acceptance transaction.
        # The injectable clock is only for deterministic snapshot tests.
        with self.connect() as db:
            now = (
                self.clock()
                if self.clock
                else db.execute("SELECT (julianday('now') - 2440587.5) * 86400.0").fetchone()[0]
            )
        observed_at = now if observed_at is None else observed_at
        if not math.isfinite(observed_at) or observed_at < 0 or observed_at > now:
            raise ValueError("Invalid audit observation time")
        if after < 0 or (through is not None and through < 0) or limit < 1:
            raise ValueError("Invalid audit cursor")
        with self.connect() as db:
            db.execute("BEGIN")
            if through is None:
                through = db.execute(
                    "SELECT coalesce(max(delivery_id),0) FROM audit_visibility WHERE visible_at<=?",
                    (observed_at,),
                ).fetchone()[0]
            rows = db.execute(
                "SELECT d.id,d.data FROM deliveries d JOIN audit_visibility v ON v.delivery_id=d.id "
                "WHERE d.id>? AND d.id<=? AND v.visible_at<=? ORDER BY d.id LIMIT ?",
                (after, through, observed_at, limit + 1),
            ).fetchall()
            total = db.execute(
                "SELECT count(*) FROM audit_visibility WHERE delivery_id<=? AND visible_at<=?",
                (through, observed_at),
            ).fetchone()[0]
        items = [dict(id=row[0], **json.loads(row[1])) for row in rows[:limit]]
        next_page = None
        if len(rows) > limit:
            next_page = "/messages?" + urlencode(
                {"after": items[-1]["id"], "through": through, "observed_at": observed_at}
            )
        return {
            "items": items,
            "next": next_page,
            "snapshot": f"{through}@{observed_at!r}",
            "total": total,
            "observed_at": observed_at,
            "complete_through": observed_at - self.delay,
            "publication_delay_seconds": self.delay,
        }


def delayed_audit_handler(mailbox):
    class Audit(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/health":
                data = {"ready": True}
            elif url.path == "/messages":
                query = parse_qs(url.query)
                try:
                    data = mailbox.page(
                        after=int(query.get("after", ["0"])[0]),
                        through=int(query["through"][0]) if "through" in query else None,
                        observed_at=float(query["observed_at"][0]) if "observed_at" in query else None,
                    )
                except (ValueError, OverflowError):
                    self.send_error(400, "Invalid audit cursor")
                    return
            else:
                self.send_error(404)
                return
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Audit


def main():
    mailbox = DelayedAuditMailbox(os.environ.get("MAILBOX_DB", "/data/mailbox.sqlite"))
    smtp = SMTPServer(("0.0.0.0", 1025), ambiguous_smtp_handler(mailbox))
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), delayed_audit_handler(mailbox)).serve_forever()


if __name__ == "__main__":
    main()
