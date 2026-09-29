"""A bounded-worker chat gateway whose CPU *falls* as it saturates.

This is the misleading signal at the heart of the Slack 2021 cascade. Each
request costs real CPU, but a slow upstream makes workers block instead of work:
throughput collapses, so aggregate CPU drops even though the service is failing.
Any automation keyed on CPU therefore reads the incident backwards.

All control state lives on a persistent volume, so the injected condition
survives pod restarts and cannot be cleared by recreating the Deployment.
"""

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONTROL = Path(os.environ.get("CONTROL_PATH", "/control"))
UPSTREAM = os.environ.get("GATEWAY_UPSTREAM", "http://mattermost:8065")
WORKERS = int(os.environ.get("GATEWAY_WORKERS", "8"))
#: How long a request waits for a free worker before it is shed.
ACQUIRE_TIMEOUT = float(os.environ.get("GATEWAY_ACQUIRE_TIMEOUT", "2"))
#: Rounds of hashing per request. Sets how much CPU healthy traffic costs, which
#: is what makes the drop during saturation visible rather than noise.
CPU_ROUNDS = int(os.environ.get("GATEWAY_CPU_ROUNDS", "12"))
BLOCK = b"x" * 65536

#: How often the background sampler refreshes the CPU figure.
CPU_SAMPLE_SECONDS = 5.0

workers = threading.BoundedSemaphore(WORKERS)
state_lock = threading.Lock()
state = {"busy": 0, "completed": 0, "rejected": 0, "upstream_errors": 0, "latencies": []}
#: Sampled on a fixed cadence rather than computed per reader. A reader-consumed
#: window would mean the capacity automation and an operator reading /metrics at
#: the same moment each saw a truncated interval, or no value at all.
cpu = {"percent": 0.0}


def control(name, default):
    """Read one control value, tolerating a missing or half-written file."""
    try:
        return type(default)((CONTROL / name).read_text().strip())
    except (OSError, ValueError):
        return default


def upstream_delay_seconds():
    return max(0.0, control("upstream_delay_ms", 0.0) / 1000.0)


def sample_cpu():
    """Publish process CPU across all threads, on a fixed cadence, forever."""
    wall, used = time.monotonic(), time.process_time()
    while True:
        time.sleep(CPU_SAMPLE_SECONDS)
        now, now_used = time.monotonic(), time.process_time()
        elapsed = now - wall
        if elapsed > 0:
            cpu["percent"] = round(100.0 * (now_used - used) / elapsed, 2)
        wall, used = now, now_used


def burn():
    digest = hashlib.sha256()
    for _ in range(CPU_ROUNDS):
        digest.update(BLOCK)
    return digest.hexdigest()


def snapshot():
    # The control file is read outside the lock on purpose: this runs on every
    # request through log_message, and holding the lock across a file read would
    # serialize the whole worker pool behind it.
    delay = control("upstream_delay_ms", 0.0)
    with state_lock:
        latencies = sorted(state["latencies"])
        report = {
            "workers_total": WORKERS,
            "workers_busy": state["busy"],
            "saturation_percent": round(100.0 * state["busy"] / WORKERS, 2),
            "completed": state["completed"],
            "rejected": state["rejected"],
            "upstream_errors": state["upstream_errors"],
            "upstream_delay_ms": delay,
        }
    if latencies:
        report["upstream_p50_ms"] = round(latencies[len(latencies) // 2] * 1000, 1)
        report["upstream_max_ms"] = round(latencies[-1] * 1000, 1)
    report["cpu_percent"] = cpu["percent"]
    return report


class Gateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """Saturation and latency are only visible here, not on the dashboard."""
        report = snapshot()
        print(
            f"gateway busy={report['workers_busy']}/{WORKERS} "
            f"rejected={report['rejected']} upstream_delay_ms={report['upstream_delay_ms']} "
            f"{fmt % args}",
            flush=True,
        )

    def reply(self, code, body, content_type="application/json"):
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            # Liveness deliberately ignores saturation: a gateway that is shedding
            # every request still answers here, as the real one did.
            return self.reply(200, {"status": "ok"})
        if self.path == "/metrics":
            return self.reply(200, snapshot())
        return self.proxy()

    def do_POST(self):
        return self.proxy()

    def proxy(self):
        if not workers.acquire(timeout=ACQUIRE_TIMEOUT):
            with state_lock:
                state["rejected"] += 1
            return self.reply(503, {"error": "no gateway worker available"})
        with state_lock:
            state["busy"] += 1
        started = time.monotonic()
        try:
            burn()
            # The upstream is slow, so the worker blocks here without using CPU.
            time.sleep(upstream_delay_seconds())
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            request = urllib.request.Request(
                UPSTREAM + self.path,
                data=body,
                method=self.command,
                headers={k: v for k, v in self.headers.items() if k.lower() in ("content-type", "authorization")},
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload, code = response.read(), response.status
                    content_type = response.headers.get("Content-Type", "application/json")
            except urllib.error.HTTPError as exc:
                payload, code = exc.read(), exc.code
                content_type = exc.headers.get("Content-Type", "application/json")
            with state_lock:
                state["completed"] += 1
            self.reply(code, payload, content_type)
        except Exception as exc:
            with state_lock:
                state["upstream_errors"] += 1
            self.reply(502, {"error": str(exc)})
        finally:
            elapsed = time.monotonic() - started
            with state_lock:
                state["busy"] -= 1
                state["latencies"].append(elapsed)
                del state["latencies"][:-200]
            workers.release()


if __name__ == "__main__":
    CONTROL.mkdir(parents=True, exist_ok=True)
    print(f"gateway workers={WORKERS} upstream={UPSTREAM} control={CONTROL}", flush=True)
    threading.Thread(target=sample_cpu, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Gateway).serve_forever()
