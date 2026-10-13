"""Run inside the built Stripe image: python /checks.py (no test dependencies)."""

import asyncio
import json
import os
import sys
import unittest
from collections import deque
from contextlib import asynccontextmanager
from unittest.mock import patch

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_URL", "postgresql://unused/local-test")
os.environ.setdefault("STRIPE_SK", "sk_test_serialization_only")

import backend
from app.test_cards import _CARDS, CardOutcome


class Serialization(unittest.TestCase):
    def test_json_roundtrip_preserves_all_reference_card_outcomes_and_idempotency(self):
        original = {
            "cards": _CARDS,
            "idempotency": {("api", "request"): {"body": b'{"id":"payment"}'}},
            "scopes": {"read", "write"},
            "order": deque(["first", "second"]),
        }
        restored = backend.decode(json.loads(json.dumps(backend.encode(original))))
        self.assertEqual(restored, original)
        self.assertIsInstance(restored["cards"]["4242424242424242"], CardOutcome)

    def test_user_metadata_cannot_be_interpreted_as_a_serialization_tag(self):
        original = {"metadata": {"@type": "CardOutcome", "value": {"arbitrary": "user input"}}}
        self.assertEqual(backend.decode(json.loads(json.dumps(backend.encode(original)))), original)


class Acknowledgements(unittest.IsolatedAsyncioTestCase):
    async def test_response_is_not_sent_until_database_commit(self):
        events = []

        @asynccontextmanager
        async def transaction(store):
            yield
            events.append("committed")

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"ok"})

        async def send(message):
            self.assertIn("committed", events)
            events.append(message["type"])

        with patch.object(backend, "transaction", transaction):
            await backend.DurableAPI(app, object())({"type": "http"}, None, send)
        self.assertEqual(events, ["committed", "http.response.start", "http.response.body"])

    async def test_commit_failure_cannot_acknowledge_a_payment(self):
        responses = []

        @asynccontextmanager
        async def transaction(store):
            yield
            raise RuntimeError("Primary disconnected before commit")

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"payment acknowledged"})

        async def send(message):
            responses.append(message)

        with patch.object(backend, "transaction", transaction), self.assertLogs(backend.LOG, level="ERROR"):
            await backend.DurableAPI(app, object())({"type": "http"}, None, send)
        self.assertEqual(responses[0]["status"], 503)
        self.assertNotIn(b"payment acknowledged", responses[1]["body"])

    async def test_cancellation_before_commit_sends_no_acknowledgement(self):
        responses = []

        @asynccontextmanager
        async def transaction(store):
            yield
            raise asyncio.CancelledError()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})

        async def send(message):
            responses.append(message)

        with patch.object(backend, "transaction", transaction), self.assertRaises(asyncio.CancelledError):
            await backend.DurableAPI(app, object())({"type": "http"}, None, send)
        self.assertEqual(responses, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
