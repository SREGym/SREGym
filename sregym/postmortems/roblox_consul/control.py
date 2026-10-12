"""Operational control surface, load generator, and isolated fault controller.

No Docker socket, host filesystem, or arbitrary command execution is exposed.
The runner-only endpoints require a random per-run token; operational actions
cannot edit fault state, traces, player ground truth, or grading thresholds.
"""

import concurrent.futures
import hashlib
import hmac
import json
import os
import random
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import psycopg
import redis
from common import Server, body, request, respond
from model import TIERS, bolt_delay, expected_player, streaming_delay, validate_config

ROOT = Path("/state")
ROOT.mkdir(exist_ok=True)
TIER_NAME = os.environ.get("TIER", "small")
TIER = TIERS[TIER_NAME]
TOKEN = Path("/run/secrets/runner_token").read_text().strip()
LOCK = threading.RLock()
STREAM_LOCK = threading.Lock()
EVENTS = deque(maxlen=1000)
SAMPLES = deque(maxlen=3600)
COUNTERS = {"success": 0, "maintenance": 0, "failed": 0, "incorrect": 0}
STATE = {}


def consul(path, method="GET", data=None, node="consul-1", raw=False):
    return request(f"http://{node}:8500/v1/{path}", method, data, timeout=5, raw=raw)


def peers():
    return consul("operator/raft/configuration")["Servers"]


def leader():
    return next(p["Node"] for p in peers() if p["Leader"])


def kv_get(key):
    return json.loads(consul(f"kv/{key}?raw&consistent", raw=True))


def kv_put(key, value):
    return consul(f"kv/{key}", "PUT", json.dumps(value).encode())


def save():
    with LOCK:
        temp = ROOT / "state.tmp"
        temp.write_text(json.dumps(STATE))
        temp.replace(ROOT / "state.json")


def event(kind, **fields):
    entry = {"time": time.time(), "kind": kind, **fields}
    with LOCK:
        EVENTS.append(entry)
        with (ROOT / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(entry) + "\n")
    return entry


def cache(host):
    return redis.Redis(host=host, socket_connect_timeout=0.3, socket_timeout=0.3, decode_responses=True)


def initialize():
    if (ROOT / "state.json").exists():
        STATE.update(json.loads((ROOT / "state.json").read_text()))
        for filename, target in (("events.jsonl", EVENTS), ("telemetry.jsonl", SAMPLES)):
            path = ROOT / filename
            if path.exists():
                for line in path.read_text().splitlines():
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # A crash can leave a truncated last append.
                    target.append(record)
                    if filename == "telemetry.jsonl":
                        for key in COUNTERS:
                            COUNTERS[key] += record[key]
        return
    for _ in range(90):
        try:
            if len(peers()) != 3:
                raise RuntimeError("waiting for quorum")
            with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS players (id integer PRIMARY KEY, name text NOT NULL, coins integer NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS game_sessions (id bigserial PRIMARY KEY, player_id integer REFERENCES players(id), created_at timestamptz DEFAULT now())"
                )
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO players VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        [(i, f"player-{i}", 1000 + i) for i in range(TIER["players"])],
                    )
            for host in ("cache-a", "cache-b"):
                cache(host).ping()
            break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("backend readiness timed out")
    config = {
        "streaming": True,
        "routing_nodes": TIER["routing_nodes"],
        "churn_per_second": TIER["churn"],
        "health_interval": 1,
        "admission_percent": 100,
        "cache_generation": 1,
    }
    STATE.update(
        config=config,
        active=False,
        injected_at=None,
        mode=None,
        free_pages={},
        drained=[],
        status_updates=[],
        expert_calls=0,
    )
    kv_put("platform/signing-key", hashlib.sha256(TOKEN.encode()).hexdigest())
    kv_put("platform/region", "lab-dc")
    allocations = {str(i): {"host": ("cache-a", "cache-b")[i % 2], "generation": 1} for i in range(TIER["shards"])}
    kv_put("scheduler/allocations", allocations)
    # Snapshot contains old scheduler state; a subsequent generation change makes
    # restoring it a consequential operation rather than a generic repair button.
    (ROOT / "pre-incident.snap").write_bytes(consul("snapshot", raw=True))
    warm_cache()
    save()
    event("ready", tier=TIER_NAME)


def delay_dependency():
    with LOCK:
        config = dict(STATE["config"])
        active = STATE["active"]
        pages = dict(STATE["free_pages"])
    current = leader()
    stream_time = streaming_delay(config, TIER, active)
    if stream_time:
        # Shared serialization resource models fanout contention. Bounded waits
        # avoid unbounded thread accumulation under a reconnect storm.
        if not STREAM_LOCK.acquire(timeout=0.15):
            raise TimeoutError("catalog subscription queue deadline exceeded")
        try:
            time.sleep(stream_time)
        finally:
            STREAM_LOCK.release()
    disk_time = bolt_delay(pages.get(current, 0))
    if disk_time:
        time.sleep(disk_time)
    return current


def dependency():
    delay_dependency()
    allocations = kv_get("scheduler/allocations")
    secret = kv_get("platform/signing-key")
    with LOCK:
        config = dict(STATE["config"])
    if any(a["generation"] != config["cache_generation"] for a in allocations.values()):
        raise RuntimeError("scheduler allocation generation disagrees with running cache deployment")
    if len(allocations) != TIER["shards"]:
        raise RuntimeError("cache allocation set incomplete")
    return {
        "allocations": allocations,
        "secret": secret,
        "admission_percent": config["admission_percent"],
        "players": TIER["players"],
    }


def warm_cache():
    allocations = kv_get("scheduler/allocations")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute("SELECT id,name,coins FROM players ORDER BY id").fetchall()
    for player_id, name, coins in rows:
        host = allocations[str(player_id % TIER["shards"])]["host"]
        cache(host).set(f"player:{player_id}", json.dumps({"id": player_id, "name": name, "coins": coins}))


def inject(mode, seed):
    with LOCK:
        if STATE["active"]:
            raise ValueError("already injected; use a fresh run for reset")
    candidates = [f"consul-{i}" for i in range(1, 4)]
    random.Random(seed).shuffle(candidates)
    bad = candidates[: TIER["bad_nodes"]]
    transfer(bad[0])
    with LOCK:
        STATE.update(active=True, injected_at=time.time(), mode=mode, free_pages={node: 950_000 for node in bad})
        STATE["config"].update(admission_percent=0, cache_generation=2)
    for host in ("cache-a", "cache-b"):
        cache(host).flushdb()
    if mode == "historical":
        consul("snapshot", "PUT", (ROOT / "pre-incident.snap").read_bytes(), raw=True)
        # Historical reset leaves stale allocation metadata and an unhealthy
        # worker still marked available. This is lab fixture data, not a claim
        # about the names or exact records at Roblox.
        allocations = kv_get("scheduler/allocations")
        allocations["0"]["host"] = "cache-dead"
        kv_put("scheduler/allocations", allocations)
    else:
        # Pre-reset intervention: stopping propagation can avoid stale scheduler
        # state. Caches remain cold after the initial loss of discovery.
        allocations = kv_get("scheduler/allocations")
        for entry in allocations.values():
            entry["generation"] = 2
        kv_put("scheduler/allocations", allocations)
    save()
    event("incident_started", mode=mode)
    return {"injected": True, "mode": mode}


def transfer(node):
    candidates = peers()
    target = next((p for p in candidates if p["Node"] == node), None)
    if target is None:
        raise ValueError("unknown voter")
    if not target["Leader"]:
        consul("operator/raft/transfer-leader?id=" + urllib.parse.quote(target["ID"]), "POST")
    return {"leader": leader()}


def profile(node):
    if node not in {p["Node"] for p in peers()}:
        raise ValueError("unknown node")
    with LOCK:
        pages = STATE["free_pages"].get(node, 0)
        delay = streaming_delay(STATE["config"], TIER, STATE["active"])
    return {
        "node": node,
        "leader": node == leader(),
        "provenance": "laboratory fault-model profile; not a native Consul pprof capture",
        "stream_subscription_contention_ms": round(delay * 1000),
        "bolt_free_pages": pages,
        "freelist_rewrite_bytes_per_append": pages * 8,
        "estimated_log_append_ms": round(bolt_delay(pages) * 1000),
    }


def reconcile():
    delay_dependency()
    with LOCK:
        drained = list(STATE["drained"])
        generation = STATE["config"]["cache_generation"]
    # The historical scheduler ranks the apparently empty worker first. An
    # operator must inspect/drain that worker; retrying the same job cannot fix it.
    eligible = [h for h in ("cache-dead", "cache-a", "cache-b") if h not in drained]
    selected = [eligible[i % len(eligible)] for i in range(TIER["shards"])] if eligible else []
    if not selected:
        raise ValueError("no eligible workers")
    for host in selected:
        try:
            cache(host).ping()
        except Exception as exc:
            event("scheduler_error", host=host, error="worker unreachable despite advertised free slots")
            raise RuntimeError(f"allocation failed on {host}; inspect worker health") from exc
    allocations = {str(i): {"host": host, "generation": generation} for i, host in enumerate(selected)}
    kv_put("scheduler/allocations", allocations)
    return allocations


def operate(action, args):
    if action == "help":
        return {
            "actions": {
                "status": {},
                "config": {"changes": "optional config object"},
                "metrics": {},
                "logs": {},
                "alerts": {},
                "tickets": {},
                "chat": {},
                "runbook": {},
                "raft": {},
                "profile": {"node": "consul-1"},
                "transfer": {"node": "consul-2"},
                "compact": {"node": "a non-leader"},
                "kv-get": {"key": "scheduler/allocations"},
                "kv-put": {"key": "scheduler/allocations", "value": "JSON value"},
                "snapshot-save": {},
                "snapshot-restore": {},
                "workers": {},
                "drain": {"node": "cache worker"},
                "reconcile": {},
                "cache-flush": {},
                "cache-warm": {"batch": "integer 1..20", "offset": "player offset"},
                "status-update": {"text": "incident update"},
                "expert": {"topic": "consul|scheduler|cache"},
            }
        }
    if action == "alerts":
        with LOCK:
            recent = list(SAMPLES)[-10:]
            admission = STATE["config"]["admission_percent"]
        alerts = []
        if admission < 100:
            alerts.append({"service": "player-join", "signal": "maintenance admission active", "percent": admission})
        if any(sample["failed"] for sample in recent):
            alerts.append(
                {"service": "player-join", "signal": "recent synthetic joins failed; inspect local service logs"}
            )
        if request("http://gateway:8080/metrics")["origin_overloads"]:
            alerts.append({"service": "origin", "signal": "read capacity exceeded during this episode"})
        return alerts
    if action == "tickets":
        with LOCK:
            recent = list(SAMPLES)[-10:]
            recovered = len(recent) == 10 and all(s["admission"] == 100 and s["failed"] == 0 for s in recent)
        return [
            {
                "id": "SUPPORT-001",
                "source": "synthetic player-support fixture",
                "report": "Players cannot join experiences reliably. Inventory preservation is essential.",
                "status": "monitoring" if recovered else "open",
            }
        ]
    if action == "chat":
        with LOCK:
            return {"channel": "local simulated incident room", "messages": list(STATE["status_updates"])}
    if action == "runbook":
        return {
            "consul": "Measure local profiles on all voters. Take a snapshot before logical state changes. Transfer leadership before follower maintenance. Test more than the current leader.",
            "scheduler": "Compare deployment generation and allocation records. Probe workers rather than trusting advertised free capacity. Drain unreachable workers before deployment.",
            "admission": "The maintenance page is not recovery. Observe the origin budget and cache hits while warming or admitting cohorts. Verify correct joins at full admission.",
        }
    if action == "status":
        return {
            "tier": TIER_NAME,
            "config": STATE["config"],
            "traffic": dict(COUNTERS),
            "recent": list(SAMPLES)[-10:],
            "status_updates": STATE["status_updates"],
        }
    if action == "config":
        if args.get("changes"):
            with LOCK:
                STATE["config"] = validate_config(args["changes"], STATE["config"], TIER)
                save()
        return dict(STATE["config"])
    if action == "metrics":
        # Deliberate circular telemetry dependency; local logs/profile survive.
        started = time.monotonic()
        delay_dependency()
        if time.monotonic() - started > 0.3:
            raise TimeoutError("telemetry service discovery deadline exceeded; local profiles remain available")
        return {"traffic": dict(COUNTERS), "samples": list(SAMPLES)[-30:], "consul": consul("agent/metrics")}
    if action == "logs":
        return list(EVENTS)[-80:]
    if action == "raft":
        return peers()
    if action == "profile":
        return profile(args["node"])
    if action == "transfer":
        return transfer(args["node"])
    if action == "compact":
        node = args["node"]
        if node not in {p["Node"] for p in peers()} or node == leader():
            raise ValueError("compact only a known follower; transfer leadership first")
        # Model a bounded maintenance operation, not upstream file compaction.
        time.sleep(2)
        with LOCK:
            STATE["free_pages"][node] = 0
            save()
        return {"node": node, "maintenance": "modeled freelist compaction complete"}
    if action in {"kv-get", "kv-put"}:
        key = args["key"]
        if key not in {"scheduler/allocations", "platform/region"}:
            raise ValueError("key outside operational scope")
        return kv_get(key) if action == "kv-get" else kv_put(key, args["value"])
    if action == "snapshot-save":
        (ROOT / "operator.snap").write_bytes(consul("snapshot", raw=True))
        return {"saved": True}
    if action == "snapshot-restore":
        path = ROOT / "operator.snap"
        if not path.exists():
            path = ROOT / "pre-incident.snap"
        consul("snapshot", "PUT", path.read_bytes(), raw=True)
        return {"restored": path.name, "note": "verify allocation metadata against deployed workers"}
    if action == "workers":
        result = []
        for host in ("cache-dead", "cache-a", "cache-b"):
            try:
                healthy = bool(cache(host).ping())
            except Exception:
                healthy = False
            result.append(
                {
                    "node": host,
                    "scheduler_free_slots": 100 if host == "cache-dead" else 10,
                    "reachable": healthy,
                    "drained": host in STATE["drained"],
                }
            )
        return result
    if action == "drain":
        if args["node"] not in {"cache-dead", "cache-a", "cache-b"}:
            raise ValueError("unknown worker")
        with LOCK:
            if args["node"] not in STATE["drained"]:
                STATE["drained"].append(args["node"])
            save()
        return {"drained": STATE["drained"]}
    if action == "reconcile":
        return reconcile()
    if action == "cache-flush":
        for host in ("cache-a", "cache-b"):
            cache(host).flushdb()
        return {"flushed": True}
    if action == "cache-warm":
        batch, offset = args.get("batch", 10), args.get("offset", 0)
        if (
            type(batch) is not int
            or not 1 <= batch <= 20
            or type(offset) is not int
            or not 0 <= offset < TIER["players"]
        ):
            raise ValueError("invalid batch/offset")
        # Uses the same budgeted data path as users; it cannot bypass discovery,
        # broken allocations, or the origin's capacity limits.
        warmed = 0
        for i in range(offset, min(offset + batch, TIER["players"])):
            request(f"http://gateway:8080/warm?player={i}", timeout=3)
            warmed += 1
            time.sleep(0.15)
        return {"warmed": warmed, "next_offset": offset + warmed}
    if action == "status-update":
        text = args.get("text", "")
        if not isinstance(text, str) or not 1 <= len(text) <= 2000:
            raise ValueError("status text must be 1..2000 characters")
        with LOCK:
            STATE["status_updates"].append({"time": time.time(), "text": text})
            save()
        return {"recorded": True}
    if action == "expert":
        with LOCK:
            STATE["expert_calls"] += 1
            save()
            if STATE["expert_calls"] > 6:
                return {"reply": "Specialist unavailable; consult local runbooks and measurements."}
        replies = {
            "consul": "Compare local profiles across leader changes. A cluster snapshot restores logical state, not the local log-store layout or client behavior.",
            "scheduler": "Compare actual worker reachability with advertised capacity and allocation generations before retrying deployment.",
            "cache": "Origin has a small read budget. Admission and cache warming share it. Inspect errors after every traffic increase.",
        }
        return {"reply": replies.get(args.get("topic"), "I can advise on consul, scheduler, or cache.")}
    raise ValueError("unknown action; use help")


def workload():
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=128)
    tick = 0
    rng = random.Random(73821)

    def join(player_id):
        try:
            result = request(f"http://gateway:8080/join?player={player_id}", timeout=2)
            key = "incorrect" if result["player"] != expected_player(player_id) else "success"
        except Exception as exc:
            key = "maintenance" if getattr(exc, "code", None) == 503 and exc.headers.get("X-Maintenance") else "failed"
        with LOCK:
            COUNTERS[key] += 1

    while True:
        start = time.monotonic()
        with LOCK:
            before = dict(COUNTERS)
        futures = [pool.submit(join, rng.randrange(TIER["players"])) for _ in range(TIER["rps"])]
        concurrent.futures.wait(futures, timeout=3)
        with LOCK:
            sample = {
                "time": time.time(),
                "admission": STATE["config"]["admission_percent"],
                **{k: COUNTERS[k] - before[k] for k in COUNTERS},
            }
            SAMPLES.append(sample)
            with (ROOT / "telemetry.jsonl").open("a") as stream:
                stream.write(json.dumps(sample) + "\n")
        if tick % 10 == 0:
            event("traffic", **sample)
        tick += 1
        time.sleep(max(0, 1 - (time.monotonic() - start)))


def catalog_churn():
    # Real catalog registrations/KV writes coexist with the explicit latency
    # model. Player admission does not stop this internal control-plane load.
    cursor = 0
    while True:
        try:
            with LOCK:
                config = dict(STATE["config"])
            for _ in range(config["churn_per_second"]):
                slot = cursor % config["routing_nodes"]
                consul(
                    "agent/service/register",
                    "PUT",
                    {
                        "ID": f"router-{slot}",
                        "Name": "routing",
                        "Address": "gateway",
                        "Port": 8080,
                        "Meta": {"revision": str(cursor)},
                    },
                )
                kv_put(f"routing/heartbeat/{slot}", cursor)
                cursor += 1
            time.sleep(config["health_interval"])
        except Exception as exc:
            event("catalog_error", error=str(exc))
            time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        try:
            if self.path == "/health":
                respond(self, {"ready": bool(STATE)})
            elif self.path == "/admission":
                respond(self, {"percent": STATE["config"]["admission_percent"]})
            elif self.path == "/dependency":
                respond(self, dependency())
            else:
                respond(self, {"error": "not found"}, 404)
        except Exception as exc:
            event("dependency_error", error=str(exc))
            respond(self, {"error": str(exc)}, 503)

    def do_POST(self):
        try:
            data = body(self)
            if self.path.startswith("/runner/"):
                supplied = self.headers.get("Authorization", "")
                if not hmac.compare_digest(supplied, "Bearer " + TOKEN):
                    respond(self, {"error": "runner authentication required"}, 403)
                    return
                if self.path == "/runner/inject":
                    if data.get("mode") not in {"historical", "intervention"}:
                        raise ValueError("invalid incident mode")
                    result = inject(data["mode"], data.get("seed", 0))
                elif self.path == "/runner/evidence":
                    result = {"state": STATE, "samples": list(SAMPLES), "events": list(EVENTS)}
                else:
                    respond(self, {"error": "not found"}, 404)
                    return
            elif self.path == "/ops":
                action, args = data.get("action"), data.get("args", {})
                event("action", action=action, args=args)
                result = operate(action, args)
            else:
                respond(self, {"error": "not found"}, 404)
                return
            respond(self, result)
        except (ValueError, KeyError, TypeError) as exc:
            respond(self, {"error": str(exc)}, 400)
        except Exception as exc:
            event("operation_error", error=str(exc))
            respond(self, {"error": str(exc)}, 503)


if __name__ == "__main__":
    initialize()
    threading.Thread(target=workload, daemon=True).start()
    threading.Thread(target=catalog_churn, daemon=True).start()
    Server(("0.0.0.0", 8080), Handler).serve_forever()
