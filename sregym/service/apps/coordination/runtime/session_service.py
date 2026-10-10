"""A customer-facing service whose availability tracks coordination capacity.

This is what the incident actually costs: every request resolves its backend
through the coordination service, so the fraction it can serve is the fraction
coordination can serve. It reports its own metrics honestly and directly, which
is the path around the circular observability failure.
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COORDINATOR = os.environ.get("COORDINATOR_URL", "http://coordinator:8080")
SERVICE = os.environ.get("SERVICE_NAME", "session-service")

lock = threading.Lock()
counters = {"served": 0, "failed": 0, "lookups": 0}
#: Refreshed from the coordinator rather than read per request, so a burst of
#: traffic does not itself hammer the degraded leader.
capacity = {"fraction": 0.0, "at": 0.0, "admitted": 0.0}


def refresh():
    while True:
        try:
            with urllib.request.urlopen(COORDINATOR + "/v1/internal/truth", timeout=5) as response:
                truth = json.loads(response.read())
            with lock:
                counters["lookups"] += 1
                capacity["fraction"] = float(truth.get("serve_capacity_fraction") or 0.0)
                capacity["admitted"] = float(truth.get("admitted_fraction") or 0.0)
                capacity["at"] = time.time()
        except (urllib.error.URLError, OSError, ValueError):
            with lock:
                capacity["fraction"] = 0.0
        time.sleep(5)


def serves_request(rng):
    """A request succeeds only if it is admitted *and* lands on warm capacity."""
    with lock:
        admitted, served_fraction = capacity["admitted"], capacity["fraction"]
    if rng.random() > admitted:
        # Not admitted: shed at the edge, which is a deliberate operator choice
        # rather than a failure, so it is not counted against the service.
        return None
    return rng.random() <= served_fraction


class Session(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rng = random.Random(1234)

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
            # Liveness only. This service is up while serving nothing, which is
            # why a readiness probe is not a recovery signal.
            return self.reply(200, {"status": "ok", "service": SERVICE})
        if self.path == "/metrics":
            with lock:
                total = counters["served"] + counters["failed"]
                return self.reply(
                    200,
                    {
                        "service": SERVICE,
                        "served": counters["served"],
                        "failed": counters["failed"],
                        "success_rate": round(counters["served"] / total, 3) if total else None,
                        "coordination_capacity_fraction": capacity["fraction"],
                        "admitted_fraction": capacity["admitted"],
                    },
                )
        if self.path != "/session":
            return self.reply(404, {"error": "no such endpoint"})

        outcome = serves_request(self.rng)
        if outcome is None:
            return self.reply(429, {"error": "not admitted", "service": SERVICE})
        with lock:
            counters["served" if outcome else "failed"] += 1
        if not outcome:
            return self.reply(503, {"error": "backend lookup failed", "service": SERVICE})
        return self.reply(200, {"session": "ok", "service": SERVICE})


if __name__ == "__main__":
    print(f"{SERVICE} coordinator={COORDINATOR}", flush=True)
    threading.Thread(target=refresh, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Session).serve_forever()
