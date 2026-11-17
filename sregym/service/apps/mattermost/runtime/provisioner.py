"""Registers new chat-gateway instances before they may serve traffic.

A registration holds a lease file and fetches the instance's configuration from
the backend -- an index, then several documents concurrently -- then records the instance.
Every one of those is a file descriptor held for as long as the backend takes to
answer, so the descriptors a registration needs scale with backend latency, and
the process's open-files limit caps how many registrations can be in flight.
"""

import errno
import json
import os
import resource
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(os.environ.get("PROVISIONER_DATA", "/var/lib/provisioner"))
BACKEND = os.environ.get("PROVISIONER_BACKEND", "http://mattermost:8065/api/v4/system/ping")
#: Configuration documents fetched per registration.
FETCHES = int(os.environ.get("PROVISIONER_FETCHES", "4"))
BACKEND_TIMEOUT = float(os.environ.get("PROVISIONER_BACKEND_TIMEOUT", "20"))

lock = threading.Lock()
stats = {"registered": 0, "failed": 0, "in_flight": 0, "accept_errors": 0}
leases_held = {}
last_accept_error = [0.0]


def fetch(_):
    with urllib.request.urlopen(BACKEND, timeout=BACKEND_TIMEOUT) as response:
        return response.read()


def fetch_configuration(instance):
    bodies = [fetch("index")]
    with ThreadPoolExecutor(max_workers=FETCHES) as pool:
        bodies += pool.map(fetch, range(FETCHES))
    if any(json.loads(body).get("status") != "OK" for body in bodies):
        raise RuntimeError(f"backend refused configuration for {instance}")


def register(instance):
    leases = DATA / "leases"
    leases.mkdir(parents=True, exist_ok=True)
    lease = leases / f"{instance}.{uuid.uuid4().hex[:8]}"
    held = lease.open("w")
    with lock:
        leases_held[lease] = held
    held.write(f"{time.time()}\n")
    held.flush()
    fetch_configuration(instance)
    with (DATA / "registry.jsonl").open("a") as registry:
        registry.write(json.dumps({"instance": instance, "at": time.time()}) + "\n")
    with lock:
        leases_held.pop(lease).close()
    lease.unlink(missing_ok=True)


def describe(exc):
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, OSError):
        exc = exc.reason
    if isinstance(exc, OSError) and exc.errno == errno.EMFILE:
        return f"[Errno {errno.EMFILE}] Too many open files"
    return f"{type(exc).__name__}: {exc}"


class Provisioner(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def reply(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok"})
        if self.path == "/stats":
            with lock:
                return self.reply(200, dict(stats))
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/register":
            return self.reply(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        instance = json.loads(self.rfile.read(length) or b"{}").get("instance", "unknown")
        with lock:
            stats["in_flight"] += 1
        started = time.monotonic()
        try:
            register(instance)
        except Exception as exc:
            with lock:
                stats["failed"] += 1
            print(f"provision {instance} failed after {time.monotonic() - started:.1f}s: {describe(exc)}", flush=True)
            return self.reply(500, {"error": describe(exc)})
        finally:
            with lock:
                stats["in_flight"] -= 1
        with lock:
            stats["registered"] += 1
        print(f"provisioned {instance} in {time.monotonic() - started:.1f}s", flush=True)
        return self.reply(200, {"registered": instance})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256

    def get_request(self):
        try:
            return super().get_request()
        except OSError as exc:
            # socketserver drops accept errors silently and the listening socket
            # stays readable, so without this the loop would spin and nobody
            # would ever see why connections hang.
            with lock:
                stats["accept_errors"] += 1
            now = time.monotonic()
            if now - last_accept_error[0] >= 1:
                last_accept_error[0] = now
                print(f"accept failed: {describe(exc)}", flush=True)
            time.sleep(0.05)
            raise


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    print(f"provisioner backend={BACKEND} fetches={FETCHES} open_files_limit={soft}", flush=True)
    Server(("", 8080), Provisioner).serve_forever()
