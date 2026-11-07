import itertools
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = os.environ["TARGET"]
TIMEOUT = float(os.environ["TIMEOUT"])
FORWARDED = ("Content-Type", "X-Gitea-Delivery", "X-Gitea-Event", "X-Gitea-Signature")
ids = itertools.count(1)
queue = []
changed = threading.Condition()
paused = False


def forward_forever():
    while True:
        with changed:
            while paused or not queue:
                changed.wait()
            item = queue[0]
        try:
            request = urllib.request.Request(TARGET, data=item["body"], headers=item["headers"], method="POST")
            urllib.request.urlopen(request, timeout=TIMEOUT).read()
        except urllib.error.HTTPError as error:
            if error.code >= 500:
                time.sleep(1)
                continue
        except Exception:
            time.sleep(1)
            continue
        with changed:
            if queue and queue[0] is item:
                queue.pop(0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        global paused
        if self.path in ("/pause", "/resume"):
            with changed:
                paused = self.path == "/pause"
                changed.notify_all()
        elif self.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        else:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            headers = {name: self.headers[name] for name in FORWARDED if self.headers.get(name)}
            with changed:
                queue.append({"id": next(ids), "headers": headers, "body": body})
                changed.notify_all()
        self.send_response(204)
        self.end_headers()

    def do_DELETE(self):
        doomed = set(self.path.rpartition("/")[2].split(","))
        with changed:
            queue[:] = [item for item in queue if str(item["id"]) not in doomed]
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        with changed:
            items = [{"id": i["id"], "headers": i["headers"], "body": i["body"].decode()} for i in queue]
            data = json.dumps({"paused": paused, "queued": items}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


threading.Thread(target=forward_forever, daemon=True).start()
ThreadingHTTPServer(("", 8080), Handler).serve_forever()
