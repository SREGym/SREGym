"""Small executable gaming backend: discovery -> secret -> cache -> player DB.

Redis really caches PostgreSQL rows, and successful joins create durable game
sessions. The origin read budget represents reduced database capacity: cold
cache fanout has observable consequences even after Consul is repaired.
"""

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import psycopg
import redis
from common import Server, request, respond
from model import TIERS

TIER = TIERS[os.environ.get("TIER", "small")]
LOCK = threading.Lock()
ORIGIN_READS = deque()
METRICS = {"joins": 0, "cache_hits": 0, "cache_misses": 0, "origin_overloads": 0, "dependency_failures": 0}
READ_BUDGET = 8 if os.environ.get("TIER", "small") == "small" else 24

METRICS_PATH = Path("/state/metrics.json")
METRICS_PATH.parent.mkdir(exist_ok=True)
if METRICS_PATH.exists():
    METRICS.update(json.loads(METRICS_PATH.read_text()))


def persist_metrics():
    # Called under LOCK. Persist safety violations before replying, so a
    # process restart cannot erase an unsafe reconnect surge.
    temp = METRICS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(METRICS))
    temp.replace(METRICS_PATH)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health":
            respond(self, {"process": "running"})
            return
        if parsed.path == "/metrics":
            with LOCK:
                metrics = dict(METRICS)
            respond(self, {**metrics, "origin_read_budget_per_second": READ_BUDGET})
            return
        if parsed.path not in {"/join", "/warm"}:
            respond(self, {"error": "not found"}, 404)
            return
        try:
            player_id = int(urllib.parse.parse_qs(parsed.query)["player"][0])
            if not 0 <= player_id < TIER["players"]:
                raise ValueError("unknown player")
        except (ValueError, KeyError):
            respond(self, {"error": "valid player ID required"}, 400)
            return
        try:
            admission = request("http://control:8080/admission", timeout=0.5)["percent"]
            if parsed.path == "/join" and player_id % 100 >= admission:
                self.send_response(503)
                self.send_header("X-Maintenance", "true")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            dep = request("http://control:8080/dependency", timeout=0.3)
            allocation = dep["allocations"][str(player_id % TIER["shards"])]
            client = redis.Redis(host=allocation["host"], socket_connect_timeout=0.2, socket_timeout=0.2)
            value = client.get(f"player:{player_id}")
            if value is not None:
                player = json.loads(value)
                with LOCK:
                    METRICS["cache_hits"] += 1
            else:
                with LOCK:
                    METRICS["cache_misses"] += 1
                    now = time.monotonic()
                    while ORIGIN_READS and ORIGIN_READS[0] <= now - 1:
                        ORIGIN_READS.popleft()
                    if len(ORIGIN_READS) >= READ_BUDGET:
                        METRICS["origin_overloads"] += 1
                        persist_metrics()
                        raise RuntimeError("origin read budget exceeded: cold-cache reconnect surge")
                    ORIGIN_READS.append(now)
                with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=2) as conn:
                    row = conn.execute("SELECT id,name,coins FROM players WHERE id=%s", (player_id,)).fetchone()
                if not row:
                    raise RuntimeError("player data missing")
                player = dict(zip(("id", "name", "coins"), row, strict=True))
                client.set(f"player:{player_id}", json.dumps(player))
            session_id = None
            if parsed.path == "/join":
                with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=2) as conn:
                    session_id = conn.execute(
                        "INSERT INTO game_sessions(player_id) VALUES (%s) RETURNING id", (player_id,)
                    ).fetchone()[0]
                with LOCK:
                    METRICS["joins"] += 1
            signature = hmac.new(
                dep["secret"].encode(), f"{player_id}:{session_id}".encode(), hashlib.sha256
            ).hexdigest()
            respond(self, {"player": player, "session": session_id, "signature": signature})
        except Exception as exc:
            with LOCK:
                METRICS["dependency_failures"] += 1
            respond(self, {"error": str(exc)}, 502)


if __name__ == "__main__":
    Server(("0.0.0.0", 8080), Handler).serve_forever()
