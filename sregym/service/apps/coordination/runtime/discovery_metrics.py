"""A metrics endpoint that resolves its targets through the service it monitors.

This is the circular observability failure from the Roblox incident: the
telemetry you would reach for to diagnose the coordination service discovers its
scrape targets *through* that same coordination service. While coordination is
degraded, this endpoint returns no series at all -- not wrong numbers, nothing --
so an agent that waits for the dashboard to tell it what is wrong waits forever.

The per-service truth is still reachable by asking each service directly. The
point is that the aggregated view, which is the convenient one, is exactly the
view that fails.
"""

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COORDINATOR = os.environ.get("COORDINATOR_URL", "http://coordinator:8080")
#: Services this endpoint would report on, if it could resolve them.
TARGETS = [t for t in os.environ.get("SCRAPE_TARGETS", "").split(",") if t]


def coordination_usable():
    """Scrape targets come from the coordination service's own registry."""
    try:
        with urllib.request.urlopen(COORDINATOR + "/v1/internal/truth", timeout=5) as response:
            truth = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False, None
    # Discovery needs a leader that can actually serve reads, and placement data
    # that is not stale -- the same conditions the data plane needs.
    return bool(truth.get("leader_healthy") and truth.get("scheduler_state_fresh")), truth


def scrape(target):
    try:
        with urllib.request.urlopen(f"http://{target}/metrics", timeout=5) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Metrics(BaseHTTPRequestHandler):
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
            # Up, and reporting nothing. A green probe here is not evidence.
            return self.reply(200, {"status": "ok"})
        if self.path != "/metrics":
            return self.reply(404, {"error": "no such endpoint"})

        usable, truth = coordination_usable()
        if not usable:
            return self.reply(
                503,
                {
                    "series": [],
                    "targets_resolved": 0,
                    "error": "service discovery unavailable: cannot resolve scrape targets",
                    "note": (
                        "This collector resolves its targets through the coordination "
                        "service it is meant to monitor. Query each service directly "
                        "while coordination is degraded."
                    ),
                },
            )

        series = []
        for target in TARGETS:
            sample = scrape(target)
            if sample is not None:
                series.append({"target": target, **sample})
        return self.reply(
            200,
            {
                "series": series,
                "targets_resolved": len(series),
                "targets_configured": len(TARGETS),
                "coordination": {
                    "leader": truth.get("leader"),
                    "serve_capacity_fraction": truth.get("serve_capacity_fraction"),
                },
            },
        )


if __name__ == "__main__":
    print(f"discovery metrics coordinator={COORDINATOR} targets={TARGETS}", flush=True)
    ThreadingHTTPServer(("", 8080), Metrics).serve_forever()
