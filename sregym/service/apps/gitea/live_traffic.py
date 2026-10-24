"""Synthetic users opening issues."""

import base64
import json
import os
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPOSITORY = os.environ["REPOSITORY"]
INTERVAL = float(os.environ["INTERVAL"])
STARTED = time.time()
acknowledged = []
lock = threading.Lock()
running = threading.Event()
running.set()


def open_issue(title):
    username = Path("/credentials/username").read_text().strip()
    password = Path("/credentials/password").read_text().strip()
    request = urllib.request.Request(
        f"http://gitea:3000/api/v1/repos/{REPOSITORY}/issues",
        data=json.dumps({"title": title, "body": "Opened from the web UI."}).encode(),
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read())["number"]


def write_forever():
    while True:
        running.wait()
        started = time.time()
        title = f"Feedback {secrets.token_hex(4)}"
        try:
            number = open_issue(title)
        except Exception:
            pass
        else:
            with lock:
                acknowledged.append({"number": number, "title": title, "at": time.time()})
        time.sleep(max(0.0, INTERVAL - (time.time() - started)))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/pause":
            running.clear()
        elif self.path == "/resume":
            running.set()
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        with lock:
            data = json.dumps({"started": STARTED, "now": time.time(), "acknowledged": acknowledged}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


threading.Thread(target=write_forever, daemon=True).start()
ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
