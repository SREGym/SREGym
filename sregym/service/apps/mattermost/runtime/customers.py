"""Open-loop customer traffic from outside the cluster.

Requests arrive at a fixed rate whatever the service does, the way users do: a
slow service is not offered less work, it accumulates it. (A closed loop of
clients would throttle itself as latency rose and hide the surge.) The rate is
read from a local file the harness writes; outcomes are published as cumulative
counters so a grader can difference any two snapshots.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STATE = Path(os.environ.get("CUSTOMERS_STATE", "/run/customers"))
TARGET = os.environ.get("CUSTOMERS_TARGET", "http://chat-gateway.mattermost.svc:8080/api/v4/system/ping")
INITIAL_RATE = float(os.environ.get("CUSTOMERS_RATE", "10"))
TIMEOUT = float(os.environ.get("CUSTOMERS_TIMEOUT", "15"))
#: Requests allowed in flight. Past this, arrivals are counted as abandoned.
IN_FLIGHT = int(os.environ.get("CUSTOMERS_IN_FLIGHT", "600"))
STATS_SECONDS = 1.0

lock = threading.Lock()
counters = {"sent": 0, "served": 0, "shed": 0, "failed": 0, "abandoned": 0, "latency_ms_total": 0.0}
slots = threading.BoundedSemaphore(IN_FLIGHT)


def classify(status, body):
    """served, shed (the gateway refused for lack of a worker) or failed."""
    if status == 200 and body.get("status") == "OK":
        return "served"
    if status == 503:
        return "shed"
    return "failed"


def request():
    started = time.monotonic()
    try:
        with urllib.request.urlopen(TARGET, timeout=TIMEOUT) as response:
            outcome = classify(response.status, json.load(response))
    except urllib.error.HTTPError as exc:
        outcome = classify(exc.code, {})
    except Exception:
        outcome = "failed"
    finally:
        slots.release()
    with lock:
        counters[outcome] += 1
        if outcome == "served":
            counters["latency_ms_total"] += (time.monotonic() - started) * 1000


def rate():
    try:
        return max(0.0, float((STATE / "rate").read_text().strip()))
    except (OSError, ValueError):
        return INITIAL_RATE


def publish():
    while True:
        with lock:
            snapshot = {"at": time.time(), "rate_per_second": rate(), **counters}
        temporary = STATE / "stats.json.tmp"
        temporary.write_text(json.dumps(snapshot))
        temporary.replace(STATE / "stats.json")
        time.sleep(STATS_SECONDS)


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    if not (STATE / "rate").exists():
        (STATE / "rate").write_text(f"{INITIAL_RATE}\n")
    threading.Thread(target=publish, daemon=True).start()
    pool = ThreadPoolExecutor(max_workers=IN_FLIGHT)
    due = time.monotonic()
    while True:
        current = rate()
        if current <= 0:
            time.sleep(0.5)
            due = time.monotonic()
            continue
        due += 1.0 / current
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -1:
            # Fell behind (a rate change, or a stalled host): do not burst.
            due = time.monotonic()
        with lock:
            counters["sent"] += 1
        if slots.acquire(blocking=False):
            pool.submit(request)
        else:
            with lock:
                counters["abandoned"] += 1


if __name__ == "__main__":
    print(f"customers target={TARGET}", flush=True)
    main()
