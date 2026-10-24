"""Scenario workload and independent reference data; deployed only outside the agent's view."""

import concurrent.futures
import fcntl
import hashlib
import hmac
import json
import os
import random
import sqlite3
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import provider
import psycopg
from confluent_kafka import Consumer, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewPartitions, NewTopic

DATA = Path(os.getenv("DATA", "/data"))
NS = os.getenv("APP_NAMESPACE", "astronomy-shop")
KAFKA = os.getenv("KAFKA_ADDR", f"kafka.{NS}:9092")
SOURCE = "order-commands"
TOPICS = (SOURCE, "order-events")
DATABASE = os.environ["DATABASE_URL"]
CONTENTION = threading.Condition()
ARRIVALS = Counter()


class EligibilityMeter:
    def __init__(self):
        self.lock = threading.Lock()
        self.skus = set()
        self.active = Counter()
        self.maximum = 0

    def reset(self, skus):
        with self.lock:
            self.skus = set(skus)
            self.active.clear()
            self.maximum = 0

    @contextmanager
    def count(self, sku):
        with self.lock:
            tracked = sku in self.skus
            if tracked:
                self.active[sku] += 1
                self.maximum = max(self.maximum, len(self.active))
        try:
            yield
        finally:
            if tracked:
                with self.lock:
                    self.active[sku] -= 1
                    if not self.active[sku]:
                        del self.active[sku]

    def peak(self):
        with self.lock:
            return self.maximum


METER = EligibilityMeter()


@contextmanager
def journal():
    connection = sqlite3.connect(DATA / "reference.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    # WAL commits remain visible across pod restarts without one disk flush per receipt.
    connection.execute("PRAGMA synchronous=NORMAL")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def database():
    deadline = time.monotonic() + 30
    while True:
        try:
            return psycopg.connect(DATABASE, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def request(service, payload):
    req = Request(f"http://{service}.{NS}:8080/", json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urlopen(req, timeout=20) as response:
        return json.load(response)


def capacity_meter(skus=None):
    body = json.dumps({"skus": skus}).encode() if skus is not None else None
    with urlopen(Request("http://localhost:8080/capacity-meter", body), timeout=5) as response:
        return json.load(response)


def initialize():
    DATA.mkdir(exist_ok=True)
    with journal() as j:
        j.executescript("""PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS skus(id TEXT PRIMARY KEY,capacity INT,eligible INT);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,order_id TEXT,body TEXT,phase TEXT,wire TEXT);
        CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,order_id TEXT,sku TEXT,event_id TEXT,eligible INT);
        CREATE TABLE IF NOT EXISTS original(id TEXT PRIMARY KEY,state TEXT);
        CREATE TABLE IF NOT EXISTS probes(id TEXT PRIMARY KEY,kind TEXT,order_id TEXT,
            started_at REAL,accepted_at REAL,applied_at REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS provider_initial(id TEXT PRIMARY KEY);
        """)


def inventory(prefix, count, capacity, eligible=True):
    ids = [f"item-{uuid.uuid4().hex}-{prefix}-{i}" for i in range(count)]
    with journal() as j, database() as db:
        for sku in ids:
            j.execute("INSERT INTO skus VALUES (?,?,?)", (sku, capacity, eligible))
            db.execute("INSERT INTO shop.skus VALUES (%s,%s)", (sku, capacity))
    return ids


def record(event, phase):
    with journal() as j:
        j.execute(
            "INSERT INTO events(id,order_id,body,phase) VALUES (?,?,?,?)",
            (event["ID"], event["Order"], json.dumps(event), phase),
        )


def create(sku, phase, cancel=False, delay=0):
    event = request("order-api", {"SKU": sku})
    record(event, phase)
    if cancel:
        time.sleep(delay)
        record(request("cancel-service", {"Order": event["Order"]}), phase)
    return event["Order"]


def settled(phase=None, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with journal() as j, database() as db:
            ids = [
                r[0]
                for r in j.execute(
                    "SELECT id FROM events" + (" WHERE phase=?" if phase else ""), (phase,) if phase else ()
                )
            ]
            actual = db.execute("SELECT count(*) FROM shop.applied_events WHERE id=ANY(%s)", (ids,)).fetchone()[0]
        if actual == len(ids):
            return True
        time.sleep(0.1)
    return False


def traffic(kind, size=200):
    phase = uuid.uuid4().hex
    hot = kind in ("contention", "history")
    skus = inventory(
        phase, 1 if kind == "history" else 8 if hot else 64, (1 if kind == "contention" else 4) if hot else size + 100
    )
    if kind == "eligibility":
        skus += inventory(phase + "-restricted", 8, size, False)
    if kind == "capacity":
        capacity_meter(skus)
    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as workers:
        futures = [
            workers.submit(
                create,
                skus[(i // max(1, size // len(skus)) if kind == "contention" else i) % len(skus)],
                phase,
                kind == "cancellation" and i % 3 == 0,
                0.15 if kind == "cancellation" and i % 2 else 0,
            )
            for i in range(size)
        ]
        orders = [future.result() for future in futures]
    if kind == "history":
        if not settled(phase):
            raise RuntimeError("History creation burst did not drain")
        capture_events()
        with database() as db, journal() as j:
            reserved = {
                r[0] for r in db.execute("SELECT id FROM shop.orders WHERE sku=%s AND state='reserved'", (skus[0],))
            }
            creates = [json.loads(r[0]) for r in j.execute("SELECT body FROM events WHERE phase=?", (phase,))]
        if len(reserved) >= 6:
            oldest = min((e for e in creates if e["Order"] in reserved), key=lambda e: e["Version"])
            record(request("cancel-service", {"Order": oldest["Order"]}), phase)
    if kind == "migration":
        if not settled(phase):
            raise RuntimeError("Pre-expansion orders did not drain")
        admin = AdminClient({"bootstrap.servers": KAFKA})
        count = len(admin.list_topics("order-events", timeout=10).topics["order-events"].partitions)
        for future in admin.create_partitions([NewPartitions(topic, count + 1) for topic in TOPICS]).values():
            future.result(30)
        time.sleep(2)  # Both producers refresh metadata every second.
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as workers:
            for event in workers.map(lambda oid: request("cancel-service", {"Order": oid}), orders):
                record(event, phase)
    if kind == "eligibility":
        if not settled(phase):
            raise RuntimeError("Eligibility decisions did not drain")
        # A cancelled rejection is still a cancellation, with its existing refund retained.
        for i, oid in enumerate(orders):
            if i % len(skus) >= 64 and i % 2 == 0:
                record(request("cancel-service", {"Order": oid}), phase)
    limit = size / 40 if kind == "capacity" else 90
    done = settled(phase, max(0.01, limit - (time.monotonic() - start)))
    elapsed = time.monotonic() - start
    with suppress(Exception):  # A capture timeout cannot change the workload's settlement verdict.
        capture_events()
    concurrency = capacity_meter() if kind == "capacity" else {}
    return {
        "phase": phase,
        "kind": kind,
        "settled": done,
        "seconds": elapsed,
        "rate": size / elapsed,
        **concurrency,
    }


def capture_events():
    records = audit()["records"]
    with journal() as j:
        pending = {
            r[0]: json.loads(r[1]) for r in j.execute("SELECT id,body FROM events") if "Version" not in json.loads(r[1])
        }
        for record in records:
            if record.get("topic", SOURCE) != SOURCE:
                continue
            body = record["body"].encode()
            signature = hmac.new(os.environ["EVENT_KEY"].encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, record["signature"]):
                continue
            try:
                event = json.loads(body)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            event = event.get("data", event)
            expected = pending.get(event.get("ID"))
            if expected and all(event.get(k) == v for k, v in expected.items()):
                j.execute(
                    "UPDATE events SET body=?,wire=? WHERE id=?", (json.dumps(event), record["body"], event["ID"])
                )
                pending.pop(event["ID"])


def snapshot():
    with database() as db, journal() as j:
        j.executemany(
            "INSERT OR IGNORE INTO original VALUES (?,?)", db.execute("SELECT id,state FROM shop.orders").fetchall()
        )


def history_plan(events, original, caps):
    """Priority applies to committed reservations, after all genuine cancellations."""
    creates = {e["Order"]: e for e in events if e["Kind"] == "create"}
    cancelled = {e["Order"] for e in events if e["Kind"] == "cancel"}
    by_sku = {}
    for oid, state in original.items():
        if state == "reserved" and oid in creates:
            by_sku.setdefault(creates[oid]["SKU"], []).append(oid)
    required, interactions = {}, []
    for sku, ids in by_sku.items():
        ids.sort(key=lambda oid: creates[oid]["Version"])
        capacity = caps[sku][0]
        remaining = [oid for oid in ids if oid not in cancelled]
        kept = set(remaining[:capacity])
        # These valid reservations would be incorrectly refunded by compensating first.
        rescued = kept - set(ids[:capacity])
        if rescued:
            interactions.append(
                {
                    "sku": sku,
                    "preserved_after_cancellation": sorted(rescued),
                    "cancelled_reservations": len(set(ids) & cancelled),
                    "excess_after_cancellation": max(0, len(remaining) - capacity),
                }
            )
        required.update(
            {oid: "cancelled" if oid in cancelled else "reserved" if oid in kept else "compensated" for oid in ids}
        )
    return required, interactions


def check():
    with journal() as j, database() as db:
        events = [json.loads(r[0]) for r in j.execute("SELECT body FROM events")]
        creates = {e["Order"]: e for e in events if e["Kind"] == "create"}
        cancelled = {e["Order"] for e in events if e["Kind"] == "cancel"}
        caps = {r[0]: (r[1], bool(r[2])) for r in j.execute("SELECT * FROM skus")}
        original = dict(j.execute("SELECT * FROM original"))
        receipts = {r[0]: tuple(r)[1:] for r in j.execute("SELECT * FROM receipts")}
        rows = {r[0]: r[1:] for r in db.execute("SELECT id,sku,cents,state,receipt FROM shop.orders")}
        refunds = dict(db.execute("SELECT * FROM shop.refunds"))
        applied = {r[0] for r in db.execute("SELECT * FROM shop.applied_events")}
    required, interactions = history_plan(events, original, caps)
    if not original:
        _, interactions = history_plan(events, {oid: row[2] for oid, row in rows.items()}, caps)
    failures, eligible, reserved = [], {}, {}
    for oid, event in creates.items():
        row = rows.get(oid)
        if row is None:
            failures.append((oid, "missing"))
            continue
        sku, cents, state, receipt = row
        expected = (oid, event["SKU"], event["ID"], int(caps[event["SKU"]][1]))
        if sku != event["SKU"] or cents != 100 or receipts.get(receipt) != expected:
            failures.append((oid, "invalid decision"))
        if state not in ("reserved", "cancelled", "rejected", "compensated"):
            failures.append((oid, "invalid state"))
        if oid in cancelled and state != "cancelled":
            failures.append((oid, "cancellation"))
        if not caps[event["SKU"]][1] and state != ("cancelled" if oid in cancelled else "rejected"):
            failures.append((oid, "ineligible"))
        if (state == "reserved" and oid in refunds) or (state != "reserved" and refunds.get(oid) != 100):
            failures.append((oid, "refund"))
        if original.get(oid) in ("rejected", "compensated") and oid not in cancelled and state != original[oid]:
            failures.append((oid, "historical state"))
        if oid in required and state != required[oid]:
            failures.append((oid, "historical priority"))
        candidate = oid not in cancelled and caps[event["SKU"]][1] and original.get(oid, "reserved") == "reserved"
        if candidate:
            eligible[sku] = eligible.get(sku, 0) + 1
        if state == "reserved":
            reserved[sku] = reserved.get(sku, 0) + 1
    for sku, (capacity, _) in caps.items():
        if reserved.get(sku, 0) != min(capacity, eligible.get(sku, 0)):
            failures.append((sku, "reservation count"))
    if any(e["ID"] not in applied for e in events):
        failures.append(("events", "not applied"))
    return {
        "success": not failures,
        "failures": failures[:20],
        "failure_count": len(failures),
        "orders": len(creates),
        "reasons": dict(Counter(reason for _, reason in failures)),
        "history_interactions": interactions,
        "overbooked_units": sum(max(0, reserved.get(sku, 0) - cap) for sku, (cap, _) in caps.items()),
    }


def payment_check(resolve=False):
    if resolve:
        capture_events()
    with database() as db, journal() as j, provider.ledger() as ledger:
        states = dict(db.execute("SELECT id,state FROM shop.orders"))
        initial = {r[0] for r in j.execute("SELECT id FROM provider_initial")}
        events = [json.loads(r[0]) for r in j.execute("SELECT body FROM events")]
        credits = [dict(r) for r in ledger.execute("SELECT * FROM refunds")]
    original = defaultdict(set)
    for event in events:
        if "Version" in event and (event["Kind"] == "create" or event["Kind"] == "cancel"):
            original[event["Order"]].add(event["Version"])
    active = defaultdict(list)
    for row in credits:
        if row["adjustment_id"] is None:
            active[row["order_id"]].append(row)
    failures = []
    for oid, state in states.items():
        expected = int(state != "reserved")
        if len(active[oid]) != expected:
            failures.append((oid, "provider missing" if len(active[oid]) < expected else "provider duplicate"))
    for oid, rows in active.items():
        if oid not in states:
            failures.append((oid, "provider stray"))
        for row in rows:
            if row["id"] not in initial and row["key"] not in {f"refund:{v}" for v in original[oid]}:
                failures.append((oid, "provider key"))
    return {
        "success": not failures,
        "failure_count": len(failures),
        "failures": failures[:20],
        "reasons": dict(Counter(reason for _, reason in failures)),
        "refunds": len(credits),
        "adjustments": sum(r["adjustment_id"] is not None for r in credits),
    }


def commands():
    evidence = audit()
    records = [r for r in evidence["records"] if r["topic"] == SOURCE] + evidence["archive"]
    result = {}
    for record in records:
        body = record["body"].encode()
        signature = hmac.new(os.environ["EVENT_KEY"].encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(signature, record["signature"]):
            document = json.loads(body)
            event = document.get("data", document)
            if isinstance(event.get("Version"), int) and event["Version"] > 0:
                result[event["ID"]] = event
    return list(result.values())


def retire():
    """Archive a closed ingress range before advancing its retention checkpoint."""
    drain()
    capture_events()
    evidence = audit()
    with database() as db, db.transaction():
        for record in evidence["records"]:
            if record["topic"] == SOURCE:
                document = json.loads(record["body"])
                event = document.get("data", document)
                db.execute(
                    "INSERT INTO shop.command_archive(id,body,signature,partition,offset_value) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (event["ID"], record["body"], record["signature"], record["partition"], record["offset"]),
                )
    targets = [TopicPartition(SOURCE, int(p), hi) for p, (lo, hi) in evidence["watermarks"][SOURCE].items() if hi]
    admin = AdminClient({"bootstrap.servers": KAFKA})
    for future in admin.delete_records(targets, request_timeout=20, operation_timeout=15).values():
        future.result(30)
    return {"retired_through": {p.partition: p.offset for p in targets}}


def replay():
    producer = Producer({"bootstrap.servers": KAFKA})
    errors = []
    with journal() as j:
        for (text,) in j.execute("SELECT wire FROM events WHERE wire IS NOT NULL"):
            document = json.loads(text)
            event = document.get("data", document)
            body = text.encode()
            signature = hmac.new(os.environ["EVENT_KEY"].encode(), body, hashlib.sha256).hexdigest()
            producer.produce(
                SOURCE,
                key=event["Order"],
                value=body,
                headers={"signature": signature},
                on_delivery=lambda err, _: errors.append(str(err)) if err else None,
            )
            producer.poll(0)
    if producer.flush(30) or errors:
        raise RuntimeError(f"replay failed: {errors}")
    drain()


def drain(timeout=120):
    """Wait for the events at the captured Kafka watermarks to commit in the shop."""
    deadline = time.monotonic() + timeout
    pending = set()
    for record in audit()["records"]:
        document = json.loads(record["body"])
        event = document.get("data", document)
        if not isinstance(event, dict) or not isinstance(event.get("ID"), str):
            raise RuntimeError("Kafka record has no order event ID")
        pending.add(event["ID"])
    while pending and time.monotonic() < deadline:
        with database() as db:
            applied = {
                row[0] for row in db.execute("SELECT id FROM shop.applied_events WHERE id=ANY(%s)", (list(pending),))
            }
        pending.difference_update(applied)
        if pending:
            time.sleep(0.2)
    if pending:
        raise RuntimeError(f"Kafka events did not apply before the deadline ({len(pending)} pending)")


def watermarks(consumer, name="order-events"):
    deadline = time.monotonic() + 30
    while True:
        try:
            topic = consumer.list_topics(name, timeout=10).topics[name]
            if topic.error:
                raise KafkaException(topic.error)
            return {p: consumer.get_watermark_offsets(TopicPartition(name, p), timeout=10) for p in topic.partitions}
        except KafkaException:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def audit():
    """Preserve input and output evidence independently of agent-editable storage."""
    consumer = Consumer(
        {"bootstrap.servers": KAFKA, "group.id": "audit-" + uuid.uuid4().hex, "enable.auto.commit": False}
    )
    try:
        bounds = {topic: watermarks(consumer, topic) for topic in TOPICS}
        positions = {(topic, p): lo for topic, parts in bounds.items() for p, (lo, hi) in parts.items()}
        records = []
        consumer.assign([TopicPartition(topic, p, lo) for (topic, p), lo in positions.items()])
        deadline = time.monotonic() + 30
        while any(positions[topic, p] < hi for topic, parts in bounds.items() for p, (lo, hi) in parts.items()):
            if time.monotonic() > deadline:
                raise RuntimeError("Kafka evidence capture timed out")
            msg = consumer.poll(0.2)
            if msg is None:
                continue
            if msg.error():
                raise RuntimeError(str(msg.error()))
            topic, partition = msg.topic(), msg.partition()
            positions[topic, partition] = msg.offset() + 1
            if msg.offset() < bounds[topic][partition][1]:
                records.append(
                    {
                        "topic": topic,
                        "partition": partition,
                        "offset": msg.offset(),
                        "body": msg.value().decode(errors="replace"),
                        "signature": next(
                            (
                                v.decode(errors="replace")
                                for k, v in msg.headers() or []
                                if k == "signature" and v is not None
                            ),
                            "",
                        ),
                    }
                )
        with database() as db:
            archive = [
                {"id": r[0], "body": r[1], "signature": r[2], "partition": r[3], "offset": r[4]}
                for r in db.execute("SELECT id,body,signature,partition,offset_value FROM shop.command_archive")
            ]
        return {"watermarks": bounds, "records": records, "archive": archive}
    finally:
        consumer.close()


class Warehouse(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, result, status=200):
        data = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.respond({"ok": True})
        if self.path == "/capacity-meter":
            return self.respond({"max_independent_inflight": METER.peak()})
        if self.path != "/summary":
            return self.respond({}, 404)
        try:
            result = check()
            payments = payment_check()
            self.respond(
                {
                    "reconciliation_discrepancies": result["failure_count"] + payments["failure_count"],
                    "reviewed_orders": result["orders"],
                }
            )
        except Exception:
            self.respond({"status": "reconciliation unavailable"}, 503)

    def do_POST(self):
        if self.path == "/capacity-meter":
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 16384)))
            METER.reset(body["skus"])
            return self.respond({"ok": True})
        if self.path != "/eligibility":
            return self.respond({}, 404)
        try:
            event = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))
            with METER.count(event["SKU"]), journal() as j:
                sku = j.execute("SELECT eligible,capacity FROM skus WHERE id=?", (event["SKU"],)).fetchone()
                # Coordinate hot-SKU probes so contention tests overlap.
                if sku and sku[1] == 1:
                    with CONTENTION:
                        ARRIVALS[event["SKU"]] += 1
                        target = (ARRIVALS[event["SKU"]] + 1) // 2 * 2
                        CONTENTION.notify_all()
                        CONTENTION.wait_for(lambda: ARRIVALS[event["SKU"]] >= target, timeout=0.2)
                time.sleep(0.017)
                receipt = uuid.uuid4().hex
                eligible = bool(sku and sku[0])
                j.execute(
                    "INSERT INTO receipts VALUES (?,?,?,?,?)",
                    (receipt, event["Order"], event["SKU"], event["ID"], eligible),
                )
            self.respond({"Receipt": receipt, "Eligible": eligible})
        except Exception:
            self.respond({}, 400)


class WarehouseServer(ThreadingHTTPServer):
    request_queue_size = 128


def live():
    while True:
        with (DATA / "live.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (DATA / "live").exists():
                try:
                    with journal() as j:
                        skus = [r[0] for r in j.execute("SELECT id FROM skus WHERE id LIKE '%-ongoing-%'")]
                    create(random.choice(skus), "ongoing", random.random() < 0.3)
                except Exception:
                    pass
        time.sleep(1)


def probe():
    """Exercise the same historical orders while the agent repairs them."""
    count = 0
    while True:
        if (DATA / "probe.start").exists() and not (DATA / "probe.stop").exists():
            with (DATA / "live.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if (DATA / "live").exists() and not (DATA / "probe.stop").exists():
                    targets = json.loads((DATA / "probe-targets.json").read_text())
                    kind = "create" if count % 3 == 2 else "cancel"
                    count += 1
                    order = random.choice(targets["orders"]) if kind == "cancel" else ""
                    service = "cancel-service" if kind == "cancel" else "order-api"
                    payload = {"Order": order} if order else {"SKU": random.choice(targets["skus"])}
                    started = time.time()
                    try:
                        event = request(service, payload)
                    except Exception as exc:
                        with journal() as j:
                            j.execute(
                                "INSERT INTO probes VALUES (?,?,?,?,?,?,?)",
                                (uuid.uuid4().hex, kind, order, started, None, None, type(exc).__name__),
                            )
                    else:
                        accepted = time.time()
                        with journal() as j:
                            j.execute(
                                "INSERT INTO events(id,order_id,body,phase) VALUES (?,?,?,?)",
                                (event["ID"], event["Order"], json.dumps(event), "live-probe"),
                            )
                            j.execute(
                                "INSERT INTO probes VALUES (?,?,?,?,?,?,?)",
                                (event["ID"], kind, event["Order"], started, accepted, None, None),
                            )
        time.sleep(1)


def sample_probes():
    while True:
        if (DATA / "probe.start").exists():
            with journal() as j:
                pending = [
                    r[0]
                    for r in j.execute("SELECT id FROM probes WHERE accepted_at IS NOT NULL AND applied_at IS NULL")
                ]
            if pending:
                try:
                    with database() as db:
                        applied = [
                            r[0] for r in db.execute("SELECT id FROM shop.applied_events WHERE id=ANY(%s)", (pending,))
                        ]
                    if applied:
                        with journal() as j:
                            j.executemany(
                                "UPDATE probes SET applied_at=? WHERE id=?", [(time.time(), i) for i in applied]
                            )
                except Exception:
                    pass  # The deadline check still counts commands the sampler cannot confirm.
        time.sleep(0.5)


def live_slo(rows, started, stopped):
    accepted = [r for r in rows if r["accepted_at"] is not None]
    errors = [r for r in rows if r["error"]]
    late = [r for r in accepted if r["applied_at"] is None or r["applied_at"] - r["accepted_at"] > 30]
    commits = sorted(r["applied_at"] for r in accepted if r["applied_at"] is not None and r["applied_at"] <= stopped)
    points = [started, *commits, stopped]
    gap = max((b - a for a, b in zip(points, points[1:], strict=False)), default=0)
    latency = max((r["applied_at"] - r["accepted_at"] for r in accepted if r["applied_at"] is not None), default=0)
    return {
        "success": bool(rows) and not errors and not late and gap <= 20,
        "commands": len(rows),
        "accepted": len(accepted),
        "intake_errors": len(errors),
        "late_or_missing": len(late),
        "max_apply_seconds": latency,
        "max_commit_gap_seconds": gap,
        "duration_seconds": stopped - started,
    }


def stop_probe():
    with (DATA / "live.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stopped = time.time()
        (DATA / "probe.stop").write_text(str(stopped))
    deadline = time.monotonic() + 32
    while time.monotonic() < deadline:
        with journal() as j:
            pending = j.execute(
                "SELECT count(*) FROM probes WHERE accepted_at IS NOT NULL AND applied_at IS NULL"
            ).fetchone()[0]
        if not pending:
            break
        time.sleep(0.5)
    with journal() as j:
        rows = [dict(r) for r in j.execute("SELECT * FROM probes ORDER BY started_at")]
    started = float((DATA / "probe.start").read_text())
    return live_slo(rows, started, stopped)


def serve():
    provider.initialize()
    for task in (provider.serve, live, probe, sample_probes):
        threading.Thread(target=task, daemon=True).start()
    WarehouseServer(("0.0.0.0", 8080), Warehouse).serve_forever()


def schema():
    with database() as db:
        db.execute(Path("/app/schema.sql").read_text())
    admin = AdminClient({"bootstrap.servers": KAFKA})
    for future in admin.create_topics([NewTopic(topic, 1, 1) for topic in TOPICS]).values():
        future.result(30)
    inventory("ongoing", 64, 100000)


def scale():
    admin = AdminClient({"bootstrap.servers": KAFKA})
    for future in admin.create_partitions([NewPartitions(topic, 6) for topic in TOPICS]).values():
        future.result(30)


def freeze():
    drain()
    capture_events()
    snapshot()
    result = check()
    cases = [case for case in result["history_interactions"] if case["excess_after_cancellation"]]
    if not cases:
        raise RuntimeError("Fixture has no cancellation/compensation interaction")
    skus = {case["sku"] for case in cases}
    with journal() as j:
        events = [json.loads(r[0]) for r in j.execute("SELECT body FROM events")]
        original = dict(j.execute("SELECT * FROM original"))
    creates = {event["Order"]: event for event in events if event["Kind"] == "create"}
    orders = sorted(
        {
            event["Order"]
            for event in events
            if event["Kind"] == "cancel"
            and original.get(event["Order"]) == "reserved"
            and creates[event["Order"]]["SKU"] in skus
        }
    )
    if not orders:
        raise RuntimeError("Fixture has no historical cancellation for live probing")
    (DATA / "probe-targets.json").write_text(json.dumps({"orders": orders, "skus": sorted(skus)}))
    with journal() as j, provider.ledger() as ledger:
        j.executemany(
            "INSERT OR IGNORE INTO provider_initial VALUES (?)",
            [(r[0],) for r in ledger.execute("SELECT id FROM refunds")],
        )
    (DATA / "live").touch()
    return result


def start_probe():
    if not (DATA / "probe-targets.json").exists():
        raise RuntimeError("Live probe targets are unavailable")
    with journal() as j:
        j.execute("DELETE FROM probes")
    (DATA / "probe.stop").unlink(missing_ok=True)
    (DATA / "probe.start").write_text(str(time.time()))
    (DATA / "live").touch()


def pause():
    (DATA / "live").unlink(missing_ok=True)
    with (DATA / "live.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
    drain()
    return {"settled": settled(timeout=120)}


def payment_wait():
    deadline = time.monotonic() + 60
    while True:
        result = payment_check()
        if result["reasons"].get("provider duplicate") and result["reasons"].get("provider missing"):
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Provider effects did not reproduce: {result['reasons']}")
        time.sleep(1)


def payment_result():
    wait = "--wait" in sys.argv
    result = payment_check(resolve=wait)
    if wait:
        deadline = time.monotonic() + 10
        while not result["success"] and time.monotonic() < deadline:
            time.sleep(0.5)
            result = payment_check(resolve=True)
    return result


def provider_fault():
    provider.disruption()
    return {"started": True}


def main():
    initialize()
    command = sys.argv[1]
    actions = {
        "serve": serve,
        "schema": schema,
        "scale": scale,
        "traffic": lambda: traffic(sys.argv[2], int(sys.argv[3])),
        "provider-fault": provider_fault,
        "freeze": freeze,
        "probe-start": start_probe,
        "probe-stop": stop_probe,
        "pause": pause,
        "check": check,
        "payment-check": payment_result,
        "payment-wait": payment_wait,
        "retire": retire,
        "replay": replay,
    }
    if command not in actions:
        raise ValueError(command)
    print(json.dumps(actions[command]() or {"ok": True}))


if __name__ == "__main__":
    main()
