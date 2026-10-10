"""A local SMTP impairment that persists while the incident is being repaired.

The repeating transport pattern is a bounded fault model, not a historical
claim: connections can fail before acceptance or after durable acceptance.
Only the selected incident traffic participates; normal and fresh probe mail
use the ordinary provider. Fault state and counters survive provider restart.
"""

import email.policy
import json
import os
import threading
from email.parser import BytesParser
from http.server import ThreadingHTTPServer

try:
    from .ambiguous_mail_sink import AcknowledgementLost, AmbiguousMailbox, ambiguous_smtp_handler, audit_handler
    from .mail_sink import SMTPServer
except ImportError:
    from ambiguous_mail_sink import AcknowledgementLost, AmbiguousMailbox, ambiguous_smtp_handler, audit_handler
    from mail_sink import SMTPServer


class IntermittentMailbox(AmbiguousMailbox):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ongoing_fault (id INTEGER PRIMARY KEY, active INTEGER, attempts INTEGER)"
            )
            db.execute("INSERT OR IGNORE INTO ongoing_fault VALUES (1,0,0)")

    def set_ongoing_fault(self, active):
        with self.connect() as db:
            # Do not reset the counter: a restart/toggle cannot erase history.
            db.execute("UPDATE ongoing_fault SET active=? WHERE id=1", (int(active),))

    def accept(self, sender, recipients, raw):
        message = BytesParser(policy=email.policy.default).parsebytes(raw)
        with self.connect() as db:
            active = db.execute("SELECT active FROM ongoing_fault WHERE id=1").fetchone()[0]
        if not active or message.get("X-GitLab-NotificationReason") != "sregym-ack":
            return super().accept(sender, recipients, raw)
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
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT active FROM fault WHERE id=1").fetchone()[0]:
                raise RuntimeError("Finite setup fault and ongoing transport fault must not overlap")
            attempt = db.execute("SELECT attempts FROM ongoing_fault WHERE id=1").fetchone()[0] + 1
            db.execute("UPDATE ongoing_fault SET attempts=? WHERE id=1", (attempt,))
            phase = attempt % 4
            delivery_id = None
            if phase != 2:
                delivery_id = db.execute("INSERT INTO deliveries(data) VALUES (?)", (json.dumps(record),)).lastrowid
            if phase in (1, 2):
                db.execute("INSERT INTO transport_faults(delivery_id) VALUES (?)", (delivery_id,))
        if phase in (1, 2):
            raise AcknowledgementLost("SMTP transport closed before success acknowledgement")


def main():
    mailbox = IntermittentMailbox(os.environ.get("MAILBOX_DB", "/data/mailbox.sqlite"))
    smtp = SMTPServer(("0.0.0.0", 1025), ambiguous_smtp_handler(mailbox))
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), audit_handler(mailbox)).serve_forever()


if __name__ == "__main__":
    main()
