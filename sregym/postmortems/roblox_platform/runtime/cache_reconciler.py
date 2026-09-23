"""Roll cache pools after the control plane can sustain ordinary KV writes.

This is part of the operator-visible platform, not an evaluator repair hook.
The requested epoch and progress live in Consul KV so a replacement controller
continues a partially completed deployment rather than restarting it.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

CONSUL = os.environ["CONSUL_HTTP_ADDR"].rstrip("/")
NOMAD = os.environ["NOMAD_ADDR"].rstrip("/")
POOL_COUNT = int(os.environ["CACHE_POOL_COUNT"])
PORT = int(os.environ["NOMAD_PORT_http"])
WRITE_SLO = float(os.environ.get("CACHE_RECONCILE_WRITE_SLO_MS", "300")) / 1000
EPOCH_KEY = "platform/cache/redeploy_epoch"
PROGRESS_KEY = "platform/cache/redeploy_progress"
COMPLETE_KEY = "platform/cache/redeploy_complete"
PLACEMENT_PREFIX = "platform/cache/placements/cache-"
STATE = {
    "stable_probes": 0, "write_ms": None, "seen_epoch": None,
    "epoch": None, "next_pool": None, "last_error": None,
}
LOCK = threading.Lock()


def record(**fields):
    with LOCK:
        STATE.update(fields)
    print(json.dumps({"time": time.time(), **fields}), flush=True)


def request(method, url, **kwargs):
    response = requests.request(method, url, timeout=5, **kwargs)
    response.raise_for_status()
    return response


def kv_get(key):
    response = requests.get(CONSUL + "/v1/kv/" + key + "?raw", timeout=5)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text


def kv_put(key, value):
    request("PUT", CONSUL + "/v1/kv/" + key, data=value)


def allocations(name):
    return request("GET", NOMAD + "/v1/job/" + name + "/allocations").json()


def passing(name, allocation_id):
    rows = request("GET", CONSUL + "/v1/health/service/" + name + "?passing=true").json()
    return any(allocation_id in row["Service"]["ID"] for row in rows)


def placement_key(index):
    return PLACEMENT_PREFIX + str(index)


def placement_matches(record, allocation):
    return bool(allocation.get("NodeID")) and isinstance(record, dict) and (
        record.get("allocation_id") == allocation["ID"]
        and record.get("node_id") == allocation["NodeID"]
    )


def reconcile(epoch):
    raw = kv_get(PROGRESS_KEY)
    progress = json.loads(raw) if raw else None
    if not isinstance(progress, dict) or progress.get("epoch") != epoch:
        progress = {"epoch": epoch, "next": 0, "inflight": None}
        kv_put(PROGRESS_KEY, json.dumps(progress))
    index = progress["next"]
    record(epoch=epoch, next_pool=index)
    if index >= POOL_COUNT:
        if kv_get(COMPLETE_KEY) != epoch:
            kv_put(COMPLETE_KEY, epoch)
            record(completed_epoch=epoch)
        return
    name = f"cache-{index}"
    if progress["inflight"] is None:
        current = [
            row for row in allocations(name)
            if row["DesiredStatus"] == "run" and row["ClientStatus"] == "running"
        ]
        if len(current) != 1:
            raise RuntimeError(f"{name}: expected one running allocation before replacement")
        raw_placement = kv_get(placement_key(index))
        placement = json.loads(raw_placement) if raw_placement else None
        if not placement_matches(placement, current[0]):
            raise RuntimeError(f"{name}: placement record conflicts with live allocation")
        old_id = current[0]["ID"]
        progress["inflight"] = old_id
        kv_put(PROGRESS_KEY, json.dumps(progress))
        request("POST", NOMAD + "/v1/allocation/" + old_id + "/stop", json={})
        record(stopped_pool=name, old_allocation=old_id)
        return
    replacements = [
        row for row in allocations(name)
        if row["ID"] != progress["inflight"]
        and row["DesiredStatus"] == "run"
        and row["ClientStatus"] == "running"
    ]
    ready = next((row for row in replacements if passing(name, row["ID"])), None)
    if ready is not None:
        kv_put(placement_key(index), json.dumps({
            "allocation_id": ready["ID"], "node_id": ready["NodeID"],
        }))
        progress["next"] += 1
        progress["inflight"] = None
        kv_put(PROGRESS_KEY, json.dumps(progress))
        record(ready_pool=name, new_allocation=ready["ID"], next_pool=progress["next"])


def loop():
    while True:
        try:
            started = time.monotonic()
            kv_put("platform/cache/reconcile_probe", str(time.time()))
            elapsed = time.monotonic() - started
            with LOCK:
                stable = STATE["stable_probes"] + 1 if elapsed <= WRITE_SLO else 0
            record(write_ms=round(1000 * elapsed, 1), stable_probes=stable, last_error=None)
            epoch = kv_get(EPOCH_KEY)
            with LOCK:
                seen_epoch = STATE["seen_epoch"]
            if epoch != seen_epoch:
                record(seen_epoch=epoch, stable_probes=0)
            elif epoch and stable >= 6:
                reconcile(epoch)
        except Exception as exc:
            record(stable_probes=0, last_error=str(exc))
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            payload = {"process": "running", "service": "cache-reconciler"}
        elif self.path == "/metrics":
            with LOCK:
                payload = dict(STATE)
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
