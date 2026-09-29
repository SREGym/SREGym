"""Durable adapter for the pinned SWE-Marathon Stripe reference implementation.

One PostgreSQL JSONB document preserves the reference's object model. Transactions
serialize API mutations with worker updates; responses are buffered until commit.
This is an intentionally bounded prototype, not a high-throughput payment store.
"""

import asyncio
import base64
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
import psycopg
from app.storage import Store
from app.test_cards import CardOutcome
from psycopg.types.json import Jsonb

LOG = logging.getLogger("marathon.persistence")
DSN = os.environ["DATABASE_URL"]
SECRET = os.environ["STRIPE_SK"]


def encode(value):
    if isinstance(value, CardOutcome):
        return {"@type": "CardOutcome", "value": asdict(value)}
    if isinstance(value, bytes):
        return {"@type": "bytes", "value": base64.b64encode(value).decode()}
    if isinstance(value, (set, tuple, deque)):
        return {"@type": type(value).__name__, "value": [encode(v) for v in value]}
    if isinstance(value, dict):
        # Escape the tag key too, so user-supplied metadata cannot become a type.
        if "@type" in value or any(not isinstance(k, str) for k in value):
            return {"@type": "mapping", "value": [[encode(k), encode(v)] for k, v in value.items()]}
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        kind = value.get("@type")
        if kind == "CardOutcome":
            return CardOutcome(**value["value"])
        if kind == "bytes":
            return base64.b64decode(value["value"])
        if kind in ("set", "tuple", "deque"):
            return {"set": set, "tuple": tuple, "deque": deque}[kind](decode(v) for v in value["value"])
        if kind == "mapping":
            return {decode(k): decode(v) for k, v in value["value"]}
        return {k: decode(v) for k, v in value.items()}
    return value


def dump(store):
    return encode({k: v for k, v in vars(store).items() if k != "lock"})


def restore(store, state):
    values = decode(state)
    for key in list(vars(store)):
        if key != "lock":
            delattr(store, key)
    for key, value in values.items():
        setattr(store, key, value)
    store.api_keys.setdefault(SECRET, {"livemode": False, "scopes": None})
    if not hasattr(store, "delivery_jobs"):
        store.delivery_jobs = {}


async def initialize():
    async with await psycopg.AsyncConnection.connect(DSN) as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(8241702)")
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS marathon_state (id integer PRIMARY KEY CHECK(id=1), state jsonb NOT NULL)"
        )
        store = Store()
        store.delivery_jobs = {}
        store.api_keys[SECRET] = {"livemode": False, "scopes": None}
        await connection.execute(
            "INSERT INTO marathon_state VALUES (1, %s) ON CONFLICT DO NOTHING", (Jsonb(dump(store)),)
        )


@asynccontextmanager
async def transaction(store):
    async with await psycopg.AsyncConnection.connect(DSN) as connection, connection.transaction():
        cursor = await connection.execute("SELECT state FROM marathon_state WHERE id=1 FOR UPDATE")
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Durable Stripe state is missing; restore the database")
        restore(store, row[0])
        yield
        await connection.execute("UPDATE marathon_state SET state=%s WHERE id=1", (Jsonb(dump(store)),))


class DurableEngine:
    """Fan-out and business changes commit in the same database transaction."""

    def __init__(self, store, retry_schedule):
        self.store = store
        self.schedule = retry_schedule

    async def start(self):
        pass

    async def stop(self):
        pass

    def fan_out(self, event):
        for endpoint in self.store.webhook_endpoints.values():
            if endpoint.get("status") != "enabled":
                continue
            if "*" not in endpoint["enabled_events"] and event["type"] not in endpoint["enabled_events"]:
                continue
            key = event["id"] + ":" + endpoint["id"]
            self.store.delivery_jobs[key] = {
                "event": event["id"],
                "endpoint": endpoint["id"],
                "attempt": 0,
                "due": time.time(),
                "lease": 0,
                "schedule": self.schedule,
            }
            event["pending_webhooks"] = event.get("pending_webhooks", 0) + 1


class RollbackResponse(Exception):
    pass


class DurableAPI:
    def __init__(self, app, store):
        self.app, self.store = app, store
        self.lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        messages = []

        async def buffer(message):
            messages.append(message)

        try:
            async with self.lock, transaction(self.store):
                await self.app(scope, receive, buffer)
                if any(m.get("status", 0) >= 500 for m in messages):
                    raise RollbackResponse()
        except RollbackResponse:
            pass
        except Exception:
            LOG.exception("Request could not commit durable state")
            messages = [
                {"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json")]},
                {
                    "type": "http.response.body",
                    "body": b'{"error":{"type":"api_error","message":"Database unavailable"}}',
                },
            ]
        for message in messages:
            await send(message)


async def idle_billing(store, on_event, stop):
    await stop.wait()


def build_api():
    # Keep imported reference files intact. Substitute only persistence and
    # background execution; business functions and API contracts stay upstream.
    from app import api, subscriptions, webhooks

    store = Store()
    store.delivery_jobs = {}
    api.Store = lambda: store
    webhooks.WebhookEngine = DurableEngine
    subscriptions.billing_worker = idle_billing
    app = api.build_app(
        secret_key=SECRET,
        idempotency_ttl=int(os.environ.get("STRIPE_IDEMPOTENCY_TTL", "86400")),
        webhook_retry_schedule=[
            int(x) for x in os.environ.get("STRIPE_WEBHOOK_RETRY_SCHEDULE", "1,2,4,8,16,32,64").split(",")
        ],
    )
    return DurableAPI(app, store)


class OneSweep:
    def __init__(self):
        self.done = False

    def is_set(self):
        return self.done

    async def wait(self):
        self.done = True


async def worker():
    from app import events, subscriptions
    from app.webhooks import _sign

    await initialize()
    store = Store()
    schedule = [int(x) for x in os.environ.get("STRIPE_WEBHOOK_RETRY_SCHEDULE", "1,2,4,8,16,32,64").split(",")]
    engine = DurableEngine(store, schedule)

    def emit(event_type, data):
        engine.fan_out(events.emit(store, event_type, data))

    while True:
        try:
            selected = None
            async with transaction(store):
                now = time.time()
                if now >= getattr(store, "next_billing_sweep", 0):
                    await subscriptions.billing_worker(store, emit, OneSweep())
                    store.next_billing_sweep = now + 1
                for key, job in list(store.delivery_jobs.items()):
                    if job["due"] > now or job["lease"] > now:
                        continue
                    event = store.events.get(job["event"])
                    endpoint = store.webhook_endpoints.get(job["endpoint"])
                    if event is None or endpoint is None or endpoint.get("status") != "enabled":
                        if event:
                            event["pending_webhooks"] = max(0, event.get("pending_webhooks", 1) - 1)
                        del store.delivery_jobs[key]
                        continue
                    job["lease"] = now + 15
                    job["attempt"] += 1
                    body = json.dumps(event, separators=(",", ":"))
                    ts = int(now)
                    selected = (
                        key,
                        dict(job),
                        endpoint["url"],
                        body,
                        _sign(store.endpoint_secrets[endpoint["id"]], ts, body),
                    )
                    break
            if not selected:
                await asyncio.sleep(0.2)
                continue
            key, claim, url, body, signature = selected
            code, text = None, ""
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.post(
                        url, content=body, headers={"Content-Type": "application/json", "Stripe-Signature": signature}
                    )
                    code, text = response.status_code, response.text[:1024]
            except httpx.RequestError as exc:
                text = type(exc).__name__
            async with transaction(store):
                job = store.delivery_jobs.get(key)
                if not job or job["lease"] != claim["lease"]:
                    continue
                succeeded = code is not None and 200 <= code < 300
                terminal = (
                    succeeded or (code is not None and 400 <= code < 500) or job["attempt"] >= len(job["schedule"])
                )
                record = {
                    "event_id": job["event"],
                    "endpoint_id": job["endpoint"],
                    "attempt": job["attempt"],
                    "sent_at": int(time.time()),
                    "status_code": code,
                    "response_body": text,
                    "status": "succeeded" if succeeded else "failed" if terminal else "retry_scheduled",
                }
                store.deliveries.setdefault(job["event"], []).append(record)
                if terminal:
                    event = store.events[job["event"]]
                    event["pending_webhooks"] = max(0, event.get("pending_webhooks", 1) - 1)
                    del store.delivery_jobs[key]
                else:
                    job["due"] = time.time() + job["schedule"][job["attempt"] - 1]
                    job["lease"] = 0
        except Exception:
            LOG.exception("Worker transaction failed; retrying retained work")
            await asyncio.sleep(1)


if __name__ == "__main__":
    import sys

    import uvicorn

    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        asyncio.run(worker())
    else:
        asyncio.run(initialize())
        uvicorn.run(build_api(), host="0.0.0.0", port=8000, log_level="warning")
