"""Local SMTP provider: durable accepted mail and a read-only audit API.

This service represents an external recipient, not an incident repair tool. Each
accepted SMTP transaction is retained, including duplicates. It never forwards
mail. Only synthetic sregym.local recipients are accepted.
"""

import email.policy
import json
import os
import socketserver
import sqlite3
import threading
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_MESSAGE = 2 * 1024 * 1024


class Mailbox:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS deliveries (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def accept(self, sender, recipients, raw):
        message = BytesParser(policy=email.policy.default).parsebytes(raw)
        body = "\n".join(
            part.get_content() for part in message.walk() if part.get_content_type() in ("text/plain", "text/html")
        )
        record = {
            "sender": sender,
            "recipients": recipients,
            "subject": str(message.get("Subject", "")),
            "body": body,
            "headers": {key.lower(): str(value) for key, value in message.items()},
        }
        with self.connect() as db:
            db.execute("INSERT INTO deliveries(data) VALUES (?)", (json.dumps(record),))

    def messages(self):
        with self.connect() as db:
            return [
                dict(id=row[0], **json.loads(row[1]))
                for row in db.execute("SELECT id,data FROM deliveries ORDER BY id")
            ]


def smtp_handler(mailbox):
    class SMTP(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(30)
            sender, recipients = None, []

            def reply(line):
                self.wfile.write(line.encode() + b"\r\n")
                self.wfile.flush()

            reply("220 mailbox.sregym.local ESMTP")
            while raw := self.rfile.readline(8193):
                if len(raw) > 8192:
                    reply("500 Command too long")
                    return
                command, _, argument = raw.decode(errors="replace").strip().partition(" ")
                command = command.upper()
                if command in ("EHLO", "HELO"):
                    reply("250 mailbox.sregym.local")
                elif command == "MAIL" and argument.upper().startswith("FROM:"):
                    sender, recipients = argument[5:].strip().split(" ")[0].strip("<>"), []
                    reply("250 OK")
                elif command == "RCPT" and sender is not None and argument.upper().startswith("TO:"):
                    recipient = argument[3:].strip().split(" ")[0].strip("<>")
                    if not recipient.lower().endswith("@sregym.local"):
                        reply("550 Only local synthetic recipients are accepted")
                    else:
                        recipients.append(recipient.lower())
                        reply("250 OK")
                elif command == "DATA" and recipients:
                    reply("354 End with <CRLF>.<CRLF>")
                    chunks, size = [], 0
                    while True:
                        line = self.rfile.readline(MAX_MESSAGE + 1)
                        if not line:
                            return
                        if line in (b".\r\n", b".\n"):
                            break
                        size += len(line)
                        if size > MAX_MESSAGE:
                            reply("552 Message too large")
                            return
                        chunks.append(line[1:] if line.startswith(b"..") else line)
                    # A success response follows the committed durable write.
                    mailbox.accept(sender, recipients, b"".join(chunks))
                    sender, recipients = None, []
                    reply("250 Stored")
                elif command == "RSET":
                    sender, recipients = None, []
                    reply("250 OK")
                elif command == "NOOP":
                    reply("250 OK")
                elif command == "QUIT":
                    reply("221 Bye")
                    return
                else:
                    reply("503 Unsupported command or invalid sequence")

    return SMTP


def http_handler(mailbox):
    class Audit(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/messages", "/health"):
                self.send_error(404)
                return
            body = json.dumps(mailbox.messages() if self.path == "/messages" else {"ready": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Audit


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    mailbox = Mailbox(os.environ.get("MAILBOX_DB", "/data/mailbox.sqlite"))
    smtp = SMTPServer(("0.0.0.0", 1025), smtp_handler(mailbox))
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), http_handler(mailbox)).serve_forever()


if __name__ == "__main__":
    main()
