import base64
import json
import os
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GITEA = os.environ["GITEA"]
REPOSITORY = os.environ["REPOSITORY"]
INTERVAL = float(os.environ["INTERVAL"])
SUBJECTS = ("Feedback", "Question", "Small bug", "Docs", "Idea")
BODIES = (
    "Opened from the web UI.",
    "Ran into this while browsing the repo.",
    "Not urgent, just noting it.",
    "Happens on the latest version too.",
)
STARTED = time.time()
acknowledged = []
lock = threading.Lock()
running = threading.Event()
running.set()


def open_issue(title, body):
    username = Path("/credentials/username").read_text().strip()
    password = Path("/credentials/password").read_text().strip()
    request = urllib.request.Request(
        f"{GITEA}/api/v1/repos/{REPOSITORY}/issues",
        data=json.dumps({"title": title, "body": body}).encode(),
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
        title = f"{secrets.choice(SUBJECTS)} {secrets.token_hex(4)}"
        body = secrets.choice(BODIES)
        try:
            number = open_issue(title, body)
        except Exception:
            pass
        else:
            with lock:
                acknowledged.append({"number": number, "title": title, "body": body, "at": time.time()})
        pause = INTERVAL * (0.75 + secrets.randbelow(51) / 100)
        time.sleep(max(0.0, pause - (time.time() - started)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

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
