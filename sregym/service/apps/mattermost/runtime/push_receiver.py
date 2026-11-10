"""Firebase Cloud Messaging, as much of it as a push relay uses.

``POST /token`` issues an access token for a service-account assertion and
``POST /v1/projects/<id>/messages:send`` accepts a message for a device. Every
accepted message is a delivered notification; deliveries are recorded once per
``ack_id`` (retries of the same notification are not new deliveries), with the
post they notify about, so a grader can match them against the posts sent.
"""

import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(os.environ.get("PUSH_STATE", "/run/push"))
SEND_PATH = re.compile(r"^/v1/projects/([^/]+)/messages:send$")

lock = threading.Lock()
counters = {"token_requests": 0, "sends": 0, "delivered": 0, "duplicates": 0, "unauthorized": 0}
seen = set()
tokens = set()


def issue_token():
    token = "ya29." + uuid.uuid4().hex
    with lock:
        counters["token_requests"] += 1
        tokens.add(token)
    return token


def deliver(message, now=None):
    """Record one send; True when it is a new delivery."""
    data = message.get("data") or {}
    ack = data.get("ack_id") or message.get("token", "") + ":" + data.get("post_id", "")
    with lock:
        counters["sends"] += 1
        if ack in seen:
            counters["duplicates"] += 1
            return False
        seen.add(ack)
        counters["delivered"] += 1
    record = {"at": time.time() if now is None else now, "ack_id": ack, "post_id": data.get("post_id")}
    with (STATE / "deliveries.jsonl").open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    return True


class Receiver(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def reply(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok"})
        return self.reply(404, {"error": {"code": 404, "status": "NOT_FOUND"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.endswith("/token"):
            return self.reply(200, {"access_token": issue_token(), "token_type": "Bearer", "expires_in": 3599})
        match = SEND_PATH.match(self.path)
        if not match:
            return self.reply(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        with lock:
            known = bearer in tokens
        if not known:
            with lock:
                counters["unauthorized"] += 1
            return self.reply(401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        try:
            message = json.loads(body or b"{}")["message"]
        except (ValueError, KeyError):
            return self.reply(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}})
        deliver(message)
        return self.reply(200, {"name": f"projects/{match.group(1)}/messages/{uuid.uuid4().int % 10**19}"})


def publish():
    while True:
        with lock:
            snapshot = {"at": time.time(), **counters}
        temporary = STATE / "stats.json.tmp"
        temporary.write_text(json.dumps(snapshot))
        temporary.replace(STATE / "stats.json")
        time.sleep(1.0)


if __name__ == "__main__":
    STATE.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=publish, daemon=True).start()
    print("push receiver listening on :8080", flush=True)
    ThreadingHTTPServer(("", 8080), Receiver).serve_forever()
