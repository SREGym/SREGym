"""Open-loop chat customers from outside the cluster: people reading and messaging.

Two arrival streams at rates the service does not control: reads of recent
messages, and direct messages to colleagues who are away from their desks and
are notified on their phones. Every request is a new HTTPS connection to the
chat edge, as customer devices make them. Outcomes are cumulative counters, and
every direct message the service accepted is recorded with its post id, so a
grader can match notifications against the messages that should cause them.
Rates are read from local files the harness writes; the accounts to use come
from the workload file mounted into the pod.
"""

import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STATE = Path(os.environ.get("CUSTOMERS_STATE", "/run/customers"))
WORKLOAD = Path(os.environ.get("CUSTOMERS_WORKLOAD", "/etc/customers/workload.json"))
CA_FILE = os.environ.get("CUSTOMERS_CA", "/etc/customers/ca.pem")
TIMEOUT = float(os.environ.get("CUSTOMERS_TIMEOUT", "15"))
IN_FLIGHT = int(os.environ.get("CUSTOMERS_IN_FLIGHT", "600"))
STATS_SECONDS = 1.0
COUNTERS = ("sent", "served", "shed", "failed", "abandoned", "latency_ms_total", "messages_sent", "messages_posted")

lock = threading.Lock()
counters = dict.fromkeys(COUNTERS, 0)
slots = threading.BoundedSemaphore(IN_FLIGHT)


def classify(status):
    """served, shed (the edge refused: over its connection limit) or failed."""
    if status in (200, 201):
        return "served"
    if status == 503:
        return "shed"
    return "failed"


def call(context, base, method, path, token, body=None):
    """``(status, parsed body or None)``; a transport failure is status 0."""
    request = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Connection": "close"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return 0, None


def record(outcome, started, message=False, post=None):
    with lock:
        counters[outcome] += 1
        if outcome == "served":
            counters["latency_ms_total"] += (time.monotonic() - started) * 1000
        if message and post:
            counters["messages_posted"] += 1
    if message and post:
        with (STATE / "posts.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": time.time(), "post_id": post["id"]}) + "\n")


def read(context, workload):
    started = time.monotonic()
    try:
        sender = random.choice(workload["senders"])
        channel = random.choice(sender["channels"])
        status, _ = call(
            context, workload["base"], "GET", f"/api/v4/channels/{channel}/posts?per_page=10", sender["token"]
        )
        record(classify(status), started)
    finally:
        slots.release()


def message(context, workload):
    started = time.monotonic()
    try:
        sender = random.choice(workload["senders"])
        channel = random.choice(sender["channels"])
        text = f"Can you take a look when you are back? ({random.randint(1000, 9999)})"
        status, post = call(
            context,
            workload["base"],
            "POST",
            "/api/v4/posts",
            sender["token"],
            {"channel_id": channel, "message": text},
        )
        outcome = classify(status)
        record(outcome, started, message=True, post=post if outcome == "served" and post else None)
    finally:
        slots.release()


def rate(name):
    try:
        return max(0.0, float((STATE / name).read_text().strip()))
    except (OSError, ValueError):
        return 0.0


def publish():
    while True:
        with lock:
            snapshot = {
                "at": time.time(),
                "rate_per_second": rate("rate"),
                "message_rate": rate("message_rate"),
                **counters,
            }
        temporary = STATE / "stats.json.tmp"
        temporary.write_text(json.dumps(snapshot))
        temporary.replace(STATE / "stats.json")
        time.sleep(STATS_SECONDS)


def arrivals(name, work, pool, context, workload, is_message=False):
    """One open-loop stream: arrivals at the rate in ``name``, whatever the service does."""
    due = time.monotonic()
    while True:
        current = rate(name)
        if current <= 0:
            time.sleep(0.5)
            due = time.monotonic()
            continue
        due += 1.0 / current
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -1:
            due = time.monotonic()
        with lock:
            counters["sent"] += 1
            if is_message:
                counters["messages_sent"] += 1
        if slots.acquire(blocking=False):
            pool.submit(work, context, workload)
        else:
            with lock:
                counters["abandoned"] += 1


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    for name in ("rate", "message_rate"):
        if not (STATE / name).exists():
            (STATE / name).write_text("0\n")
    workload = json.loads(WORKLOAD.read_text())
    context = ssl.create_default_context(cafile=CA_FILE)
    pool = ThreadPoolExecutor(max_workers=IN_FLIGHT)
    threading.Thread(target=publish, daemon=True).start()
    threading.Thread(
        target=arrivals, args=("message_rate", message, pool, context, workload, True), daemon=True
    ).start()
    arrivals("rate", read, pool, context, workload)


if __name__ == "__main__":
    print("chat customers starting", flush=True)
    main()
