"""Local webhook sink with durable, event-ID-deduplicated receipts."""

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer

DB = "/data/receipts.sqlite3"
with sqlite3.connect(DB) as connection:
    connection.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, body TEXT NOT NULL)")


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        with sqlite3.connect(DB) as connection:
            body = json.dumps([json.loads(r[0]) for r in connection.execute("SELECT body FROM receipts")]).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        event = json.loads(body)
        with sqlite3.connect(DB) as connection:
            connection.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?)", (event["id"], body.decode()))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


HTTPServer(("0.0.0.0", 8080), Receiver).serve_forever()
