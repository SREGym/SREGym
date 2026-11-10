"""Fleet monitor: an ordinary client that verifies the operator DAGs are running.

It reads the Airflow metadata database every 20 s and reports, as a user-facing
health signal, whether the minutely ``fleet_heartbeat`` pipeline is still
completing and whether task instances are getting stuck waiting to run. When a
shop database is configured it also reconciles the shop: every order placed is
shipped exactly once, and every late partner_settlement run produced a late
notice on the partner-settlement channel. With manifests enabled it also checks
that every closed carrier manifest got its carrier pickup request. It
serves the latest verdict at http://airflow-monitor:8080/status and logs a
one-line summary each cycle. It names only what a user sees -- runs not
finishing, tasks that never start -- never a cause.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import create_engine, text

DB_URL = os.environ["MONITOR_DB_URL"]
INTERVAL_S = int(os.environ.get("MONITOR_INTERVAL_S", "20"))
# A heartbeat run should finish well within a couple of minutes.
STALE_SUCCESS_S = int(os.environ.get("MONITOR_STALE_SUCCESS_S", "240"))
# A task waiting this long to leave `scheduled` is not getting run.
STUCK_SCHEDULED_S = int(os.environ.get("MONITOR_STUCK_SCHEDULED_S", "180"))
SHOP_DB_URL = os.environ.get("MONITOR_SHOP_DB_URL")
# An order should ship within a few minutes of being placed.
UNSHIPPED_S = int(os.environ.get("MONITOR_UNSHIPPED_S", "600"))
# partner_settlement reports are due two minutes after the run is queued; a late
# run must have produced a late notice within a few minutes of that.
SETTLEMENT_TARGET_S = int(os.environ.get("MONITOR_SETTLEMENT_TARGET_S", "120"))
LATE_NOTICE_GRACE_S = int(os.environ.get("MONITOR_LATE_NOTICE_GRACE_S", "240"))
# Carrier manifests: a closed manifest must have its pickup requested within a few minutes.
MANIFESTS = os.environ.get("MONITOR_MANIFESTS") == "1"
PICKUP_GRACE_S = int(os.environ.get("MONITOR_PICKUP_GRACE_S", "180"))

engine = create_engine(DB_URL, pool_pre_ping=True, pool_size=2, max_overflow=2)
shop_engine = create_engine(SHOP_DB_URL, pool_pre_ping=True, pool_size=2, max_overflow=2) if SHOP_DB_URL else None
_state = {"ok": None, "message": "starting", "checked_at": None}
_lock = threading.Lock()


def sample() -> dict:
    now = time.time()
    with engine.connect() as conn:
        last_success = conn.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM (now() - max(end_date))) FROM dag_run "
                "WHERE dag_id = 'fleet_heartbeat' AND state = 'success'"
            )
        ).scalar()
        running = conn.execute(
            text("SELECT count(*) FROM dag_run WHERE dag_id = 'fleet_heartbeat' AND state = 'running'")
        ).scalar()
        scheduled = conn.execute(text("SELECT count(*) FROM task_instance WHERE state = 'scheduled'")).scalar()
        oldest_scheduled = conn.execute(
            text("SELECT EXTRACT(EPOCH FROM (now() - min(updated_at))) FROM task_instance WHERE state = 'scheduled'")
        ).scalar()
    last_success = float(last_success) if last_success is not None else None
    oldest_scheduled = float(oldest_scheduled) if oldest_scheduled is not None else None
    problems = []
    if last_success is None or last_success > STALE_SUCCESS_S:
        age = "never" if last_success is None else f"{last_success:.0f}s ago"
        problems.append(f"fleet_heartbeat last completed {age}; {running} run(s) stuck running")
    if oldest_scheduled is not None and oldest_scheduled > STUCK_SCHEDULED_S:
        problems.append(f"{scheduled} task(s) stuck in scheduled (oldest {oldest_scheduled:.0f}s), none starting")
    shop = sample_shop(problems) if shop_engine is not None else {}
    ok = not problems
    return {
        **shop,
        "ok": ok,
        "checked_at": now,
        "heartbeat_last_success_age_s": last_success,
        "heartbeat_running": int(running),
        "scheduled_tasks": int(scheduled),
        "oldest_scheduled_age_s": oldest_scheduled,
        "message": "operator DAGs healthy" if ok else "; ".join(problems),
    }


def sample_shop(problems: list) -> dict:
    with shop_engine.connect() as conn:
        unshipped = conn.execute(
            text(
                "SELECT o.order_id, to_char(o.placed_at AT TIME ZONE 'UTC', 'HH24:MI:SS') FROM orders o "
                "WHERE o.placed_at < now() - make_interval(secs => :age) "
                "AND NOT EXISTS (SELECT 1 FROM shipments s WHERE s.order_id = o.order_id) ORDER BY o.placed_at"
            ),
            {"age": UNSHIPPED_S},
        ).all()
        duplicated = conn.execute(
            text("SELECT order_id, count(*) FROM shipments GROUP BY order_id HAVING count(*) > 1 ORDER BY order_id")
        ).all()
        noticed = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT DISTINCT run_id FROM ops_notifications "
                    "WHERE channel = 'partner-settlement' AND received_at > now() - interval '4 hours'"
                )
            )
        }
    with engine.connect() as conn:
        late_runs = conn.execute(
            text(
                "SELECT run_id, to_char(queued_at AT TIME ZONE 'UTC', 'HH24:MI:SS') FROM dag_run "
                "WHERE dag_id = 'partner_settlement' AND queued_at > now() - interval '3 hours' "
                "AND queued_at < now() - make_interval(secs => :due) "
                "AND coalesce(end_date, now()) > queued_at + make_interval(secs => :target) ORDER BY queued_at"
            ),
            {"due": SETTLEMENT_TARGET_S + LATE_NOTICE_GRACE_S, "target": SETTLEMENT_TARGET_S},
        ).all()
    unnoticed = [(run_id, queued) for run_id, queued in late_runs if run_id not in noticed]
    if unshipped:
        listed = ", ".join(f"{order} (placed {placed}Z)" for order, placed in unshipped[:10])
        problems.append(f"{len(unshipped)} order(s) placed more than {UNSHIPPED_S // 60} min ago not shipped: {listed}")
    if duplicated:
        listed = ", ".join(f"{order} x{count}" for order, count in duplicated[:10])
        problems.append(f"{len(duplicated)} order(s) shipped more than once: {listed}")
    if unnoticed:
        listed = ", ".join(f"{run_id} (queued {queued}Z)" for run_id, queued in unnoticed[:10])
        problems.append(
            f"{len(unnoticed)} partner_settlement run(s) missed the {SETTLEMENT_TARGET_S // 60}-min report target "
            f"with no late notice on partner-settlement: {listed}"
        )
    manifests = sample_manifests(problems) if MANIFESTS else {}
    return {
        **manifests,
        "unshipped_orders": [order for order, _ in unshipped],
        "duplicate_shipments": [order for order, _ in duplicated],
        "late_runs_without_notice": [run_id for run_id, _ in unnoticed],
    }


def sample_manifests(problems: list) -> dict:
    with shop_engine.connect() as conn:
        unrequested = conn.execute(
            text(
                "SELECT m.manifest_id, m.parcels, to_char(m.closed_at AT TIME ZONE 'UTC', 'HH24:MI:SS') "
                "FROM manifests m WHERE m.parcels > 0 AND m.closed_at > now() - interval '4 hours' "
                "AND m.closed_at < now() - make_interval(secs => :grace) AND NOT EXISTS ("
                "SELECT 1 FROM ops_notifications n WHERE n.channel = 'carrier-pickups' "
                "AND n.payload->>'manifest_id' = m.manifest_id::text) ORDER BY m.manifest_id"
            ),
            {"grace": PICKUP_GRACE_S},
        ).all()
        repeated = conn.execute(
            text(
                "SELECT payload->>'manifest_id', count(*) FROM ops_notifications WHERE channel = 'carrier-pickups' "
                "GROUP BY 1 HAVING count(*) > 1 ORDER BY 1"
            )
        ).all()
    if unrequested:
        listed = ", ".join(f"{m} ({p} parcels, closed {c}Z)" for m, p, c in unrequested[:10])
        problems.append(
            f"{len(unrequested)} carrier manifest(s) closed more than {PICKUP_GRACE_S // 60} min ago "
            f"with no pickup request: {listed}"
        )
    if repeated:
        listed = ", ".join(f"{m} x{count}" for m, count in repeated[:10])
        problems.append(f"{len(repeated)} manifest(s) with more than one pickup request: {listed}")
    return {
        "manifests_without_pickup": [m for m, _, _ in unrequested],
        "manifests_with_repeated_pickup": [m for m, _ in repeated],
    }


def loop():
    while True:
        try:
            verdict = sample()
        except Exception as error:  # the DB may be briefly unreachable
            verdict = {"ok": None, "message": f"monitor probe error: {error}", "checked_at": time.time()}
        with _lock:
            _state.clear()
            _state.update(verdict)
        level = "INFO" if verdict.get("ok") else "WARNING"
        print(f"{level} fleet-monitor: {verdict['message']}", flush=True)
        time.sleep(INTERVAL_S)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _lock:
            body = json.dumps(dict(_state)).encode()
        code = 200 if self.path == "/status" else 404
        self.send_response(code if self.path == "/status" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
