"""SMTP acknowledgement loss and a paginated, independently durable audit.

Fault controls are local setup methods, never HTTP endpoints. Every accepted
message remains in the same append-only ledger used by the ordinary provider.
"""

import email.policy
import json
import os
import threading
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

try:
    from .mail_sink import Mailbox, SMTPServer, smtp_handler
except ImportError:  # The two standalone modules are mounted together in the pod.
    from mail_sink import Mailbox, SMTPServer, smtp_handler


class AcknowledgementLost(ConnectionError):
    pass


class AmbiguousMailbox(Mailbox):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS fault (id INTEGER PRIMARY KEY, active INTEGER, remaining INTEGER)")
            db.execute("INSERT OR IGNORE INTO fault VALUES (1,0,0)")
            db.execute("CREATE TABLE IF NOT EXISTS transport_faults (id INTEGER PRIMARY KEY, delivery_id INTEGER)")

    def set_fault(self, accepted_before_rejecting=None):
        with self.connect() as db:
            db.execute(
                "UPDATE fault SET active=?, remaining=? WHERE id=1",
                (int(accepted_before_rejecting is not None), accepted_before_rejecting or 0),
            )

    def accept(self, sender, recipients, raw):
        message = BytesParser(policy=email.policy.default).parsebytes(raw)
        record = {
            "sender": sender,
            "recipients": recipients,
            "subject": str(message.get("Subject", "")),
            "body": "\n".join(
                part.get_content() for part in message.walk() if part.get_content_type() in ("text/plain", "text/html")
            ),
            "headers": {key.lower(): str(value) for key, value in message.items()},
        }
        with self.connect() as db:
            # Serialize acceptance and fault-budget updates across SMTP threads.
            db.execute("BEGIN IMMEDIATE")
            active, remaining = db.execute("SELECT active,remaining FROM fault WHERE id=1").fetchone()
            # Only the selected incident traffic participates in this fault.
            # Unrelated welcome/digest mail must not consume its finite budget.
            active = active and record["headers"].get("x-gitlab-notificationreason") == "sregym-ack"
            delivery_id = None
            if not active or remaining > 0:
                delivery_id = db.execute("INSERT INTO deliveries(data) VALUES (?)", (json.dumps(record),)).lastrowid
            if active:
                db.execute("UPDATE fault SET remaining=MAX(0,remaining-1) WHERE id=1")
                db.execute("INSERT INTO transport_faults(delivery_id) VALUES (?)", (delivery_id,))
        # The transaction above has committed. Closing before 250 makes client
        # failure ambiguous: some failed attempts were accepted, others were not.
        if active:
            raise AcknowledgementLost("SMTP transport closed before success acknowledgement")

    def page(self, after=0, through=None, limit=40):
        with self.connect() as db:
            if through is None:
                through = db.execute("SELECT coalesce(max(id),0) FROM deliveries").fetchone()[0]
            rows = db.execute(
                "SELECT id,data FROM deliveries WHERE id>? AND id<=? ORDER BY id LIMIT ?",
                (after, through, limit + 1),
            ).fetchall()
            total = db.execute("SELECT count(*) FROM deliveries WHERE id<=?", (through,)).fetchone()[0]
        items = [dict(id=row[0], **json.loads(row[1])) for row in rows[:limit]]
        next_page = f"/messages?after={items[-1]['id']}&through={through}" if len(rows) > limit else None
        return {"items": items, "next": next_page, "snapshot": through, "total": total}


def ambiguous_smtp_handler(mailbox):
    class SMTP(smtp_handler(mailbox)):
        def handle(self):
            try:
                super().handle()
            except AcknowledgementLost:
                return  # SocketServer closes the connection, without an SMTP reply.

    return SMTP


def audit_handler(mailbox):
    class Audit(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/health":
                data = {"ready": True}
            elif url.path == "/messages":
                query = parse_qs(url.query)
                try:
                    after = int(query.get("after", ["0"])[0])
                    through = int(query["through"][0]) if "through" in query else None
                    if after < 0 or (through is not None and through < 0):
                        raise ValueError("Negative audit cursor")
                except ValueError:
                    self.send_error(400, "Invalid audit cursor")
                    return
                data = mailbox.page(after=after, through=through)
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
    mailbox = AmbiguousMailbox(os.environ.get("MAILBOX_DB", "/data/mailbox.sqlite"))
    smtp = SMTPServer(("0.0.0.0", 1025), ambiguous_smtp_handler(mailbox))
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), audit_handler(mailbox)).serve_forever()


if __name__ == "__main__":
    main()
