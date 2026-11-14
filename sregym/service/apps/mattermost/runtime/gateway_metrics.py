"""Gateway metrics as the service publishes them: its own counters, no latency summary.

Shipped composed with ``edge_gateway``: ``snapshot`` replaces the base one.
"""
# ruff: noqa: F821 -- state_lock, state, cpu, WORKERS, UPSTREAM, sample_cpu, Gateway and threading
# come from edge_gateway, composed in.

from http.server import ThreadingHTTPServer


def snapshot():
    with state_lock:
        report = {
            "workers_total": WORKERS,
            "workers_busy": state["busy"],
            "saturation_percent": round(100.0 * state["busy"] / WORKERS, 2),
            "completed": state["completed"],
            "rejected": state["rejected"],
            "upstream_errors": state["upstream_errors"],
        }
    if "percent" in cpu:
        report["cpu_percent"] = cpu["percent"]
    return report


if __name__ == "__main__":
    print(f"gateway listening on :8080 workers={WORKERS}", flush=True)
    threading.Thread(target=sample_cpu, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Gateway).serve_forever()
