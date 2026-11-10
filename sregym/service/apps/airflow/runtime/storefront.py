"""Storefront order service.

Customers' orders are recorded in the shop database (table ``orders``) and then
published to the ``orders`` SQS queue, where the order_intake pipeline picks them
up for shipping. Serves POST /orders (place an order) and GET /orders/recent.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from sqlalchemy import create_engine, text

DB_URL = os.environ["SHOP_DB_URL"]
QUEUE_URL = os.environ["ORDERS_QUEUE_URL"]
MIN_GAP_S = float(os.environ.get("ORDER_MIN_GAP_S", "20"))
MAX_GAP_S = float(os.environ.get("ORDER_MAX_GAP_S", "50"))
SKUS = ["SKU-1001", "SKU-1002", "SKU-2040", "SKU-3300", "SKU-4511", "SKU-5007"]

engine = create_engine(DB_URL, pool_pre_ping=True, pool_size=2, max_overflow=2)
sqs = boto3.client(
    "sqs",
    endpoint_url=os.environ["SQS_ENDPOINT_URL"],
    region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
)
_lock = threading.Lock()


def place_order() -> dict:
    with _lock, engine.begin() as conn:
        number = conn.execute(text("SELECT nextval('order_number')")).scalar()
        order = {
            "order_id": f"ORD-{number:05d}",
            "sku": random.choice(SKUS),
            "qty": random.randint(1, 4),
            "amount_cents": random.randint(900, 25000),
        }
        conn.execute(
            text("INSERT INTO orders (order_id, sku, qty, amount_cents) VALUES (:order_id, :sku, :qty, :amount_cents)"),
            order,
        )
        placed_at = conn.execute(text("SELECT placed_at FROM orders WHERE order_id = :order_id"), order).scalar()
    order["placed_at"] = placed_at.isoformat()
    sent = sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(order))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE orders SET sent_at = now(), sqs_message_id = :mid WHERE order_id = :order_id"),
            {"mid": sent["MessageId"], "order_id": order["order_id"]},
        )
    print(f"INFO storefront: order {order['order_id']} placed and published", flush=True)
    return order


def loop():
    while True:
        time.sleep(random.uniform(MIN_GAP_S, MAX_GAP_S))
        try:
            place_order()
        except Exception as error:  # the queue or database may be briefly unreachable
            print(f"WARNING storefront: could not place order: {error}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/orders":
            return self._send(404, {"error": "not found"})
        try:
            return self._send(201, place_order())
        except Exception as error:
            return self._send(503, {"error": str(error)})

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path.startswith("/orders/recent"):
            with engine.connect() as conn:
                rows = (
                    conn.execute(
                        text("SELECT order_id, sku, qty, placed_at FROM orders ORDER BY placed_at DESC LIMIT 20")
                    )
                    .mappings()
                    .all()
                )
            return self._send(200, [dict(r) for r in rows])
        return self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
