"""Operator replay tool: retain original event identity and successful deliveries.

Run in stripe-worker with PYTHONPATH=/app. Optional JSON list limits event IDs.
The row lock serializes replay with API/worker mutations. Replaying an already
successful or queued event is a no-op; this never creates a payment or refund.
"""

import asyncio
import json
import sys
import time

from app.storage import Store
from backend import transaction


def requeue(store, selected=None):
    created = []
    for event_id, event in store.events.items():
        if selected is not None and event_id not in selected:
            continue
        for endpoint in store.webhook_endpoints.values():
            endpoint_id = endpoint["id"]
            if endpoint.get("status") != "enabled":
                continue
            if "*" not in endpoint["enabled_events"] and event["type"] not in endpoint["enabled_events"]:
                continue
            key = event_id + ":" + endpoint_id
            delivered = any(
                d["endpoint_id"] == endpoint_id and d["status"] == "succeeded"
                for d in store.deliveries.get(event_id, [])
            )
            if key in store.delivery_jobs or delivered:
                continue
            store.delivery_jobs[key] = {
                "event": event_id,
                "endpoint": endpoint_id,
                "attempt": 0,
                "due": time.time(),
                "lease": 0,
                "schedule": [1, 2, 4],
            }
            event["pending_webhooks"] = event.get("pending_webhooks", 0) + 1
            created.append(key)
    return created


async def main():
    store = Store()
    selected = json.loads(sys.argv[1]) if len(sys.argv) > 1 else None
    async with transaction(store):
        created = requeue(store, selected)
    print(json.dumps({"queued": len(created), "keys": created}))


if __name__ == "__main__":
    asyncio.run(main())
