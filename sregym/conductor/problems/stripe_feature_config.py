"""Cloudflare-2025-inspired configuration recurrence and payment delivery tail."""

import json
import secrets
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.stripe_config_recovery import StripeConfigRecoveryOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.stripe_config import COUNTS, StripeConfig
from sregym.utils.decorators import mark_fault_injected


class StripeFeatureConfig(Problem):
    def __init__(self, scale_tier="replicated"):
        super().__init__(StripeConfig(scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service, self.expected_service_port = self.app.slug, 8000
        self.payments, self.expected_events, self.expected_state = [], {}, None
        self.root_cause = self.build_structured_root_cause(
            component="feature-catalog",
            namespace=self.namespace,
            description="A ClickHouse permissions rollout exposes r0 and default table metadata to an unqualified feature query. Periodic oversized configuration files exceed the edge feature limit. Restore stable traffic and reconcile original failed webhook events without duplicating payments.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = StripeConfigRecoveryOracle(self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        app.set_worker(0)
        try:
            self.payments = [
                app.run_client("probe", token=secrets.token_hex(16), wait_webhook=False)
                for _ in range(COUNTS[app.scale_tier])
            ]
            state = app.state()
            ids = {p["payment"] for p in self.payments}
            self.expected_events = {
                k: v
                for k, v in state["events"].items()
                if v["type"] == "payment_intent.succeeded" and v["data"]["object"]["id"] in ids
            }
            if len(self.expected_events) != len(self.payments):
                raise RuntimeError("Accepted payments did not create one durable event each")
            self.expected_state = {
                key: state[key] for key in ("customers", "payment_intents", "charges", "refunds", "webhook_endpoints")
            }
            app.control("settings", {"enabled": False})
            app.control("sql", "GRANT SELECT ON r0.http_requests_features TO feature_new")
            app.control("generate", "feature_new")
            app.control_write("acknowledged-payments.json", json.dumps(self.payments, indent=2))
            # A change-management record: what was applied, not what it did.
            # Naming the effect ("expanded feature_new visibility") and the
            # publication cadence handed over both the cause and the mechanic.
            app.control_write(
                "change-log.txt",
                "applied: catalog access-policy migration\n",
            )
        finally:
            app.set_worker(1)
        # Model the initial bad rollout interval until the bounded retry budget
        # is exhausted, then continue mixed-policy publication autonomously.
        deadline = time.monotonic() + 180
        while True:
            state = app.state()
            if all(
                any(d["status"] == "failed" for d in state["deliveries"].get(event, []))
                for event in self.expected_events
            ):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Bad edge configuration did not exhaust acknowledged webhook deliveries")
            time.sleep(1)
        app.control("settings", {"enabled": True})

    def contain(self):
        self.app.control("settings", {"enabled": False})
        self.app.control("known-good")

    def drain(self):
        deadline = time.monotonic() + 180
        while True:
            received = {r["id"] for r in self.app.receipts()}
            deliveries = self.app.state()["deliveries"]
            if set(self.expected_events) <= received and all(
                any(d["status"] == "succeeded" for d in deliveries.get(event, [])) for event in self.expected_events
            ):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Acknowledged webhook recovery did not drain")
            time.sleep(1)

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        self.contain()
        self.app.requeue()
        self.drain()
