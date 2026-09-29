"""Reject transient recovery and lost payment/webhook state."""

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.saas import SaaSOracle
from sregym.service.apps.incident_runtime.feature_edge import PERIOD


def compare_state(expected, actual, events, receipts):
    changed = {}
    for collection, records in expected.items():
        missing = sum(actual.get(collection, {}).get(key) != value for key, value in records.items())
        if missing:
            changed[collection] = missing
    # Additional ordinary writes are allowed. New charges for an acknowledged
    # customer or extra refunds against an original charge are not recovery.
    customers = set(expected["customers"])
    original_charges = expected["charges"]
    duplicates = sum(
        k not in original_charges and v.get("customer") in customers for k, v in actual.get("charges", {}).items()
    )
    duplicates += sum(
        k not in expected["refunds"] and v.get("charge") in original_charges
        for k, v in actual.get("refunds", {}).items()
    )
    received = {r["id"]: r for r in receipts}
    queued = {job["event"] for job in actual.get("delivery_jobs", {}).values()}
    missing_events, undelivered = [], []
    for event_id, original in events.items():
        fields = ("id", "type", "data", "created")
        current = actual.get("events", {}).get(event_id, {})
        if any(current.get(k) != original.get(k) for k in fields):
            missing_events.append(event_id)
        receipt = received.get(event_id, {})
        if (
            any(receipt.get(k) != original.get(k) for k in fields)
            or event_id in queued
            or current.get("pending_webhooks", 0) != 0
            or not any(d["status"] == "succeeded" for d in actual.get("deliveries", {}).get(event_id, []))
        ):
            undelivered.append(event_id)
    return {
        "changed_records": changed,
        "duplicate_financial_effects": duplicates,
        "missing_events": missing_events,
        "undelivered_events": undelivered,
    }


class StripeConfigRecoveryOracle(SaaSOracle):
    FAILURE_CLASSES = {
        "recurring_configuration_risk": FailureClass.AGENT_ERROR,
        "acknowledged_payment_state_changed": FailureClass.AGENT_ERROR,
        "webhook_backlog_incomplete": FailureClass.AGENT_ERROR,
        "traffic_unstable": FailureClass.AGENT_ERROR,
    }
    stability_seconds = 2 * PERIOD + 2

    def evaluate(self):
        app, problem = self.problem.app, self.problem
        if not self.baseline or problem.expected_state is None:
            return super().evaluate()
        try:
            safety = app.configuration_safety()
            if not safety["safe"]:
                return self.fail("recurring_configuration_risk", configuration=safety)
            receipts = app.receipts()
            for pod in app.database_pods():
                name = pod["metadata"]["name"]
                if app.sql("SELECT to_regclass('public.marathon_state') IS NULL;", pod=name) == "t":
                    return self.fail("acknowledged_payment_state_changed", member=name, missing="state_table")
                if app.sql("SELECT count(*) FROM marathon_state WHERE id=1;", pod=name) != "1":
                    return self.fail("acknowledged_payment_state_changed", member=name, missing="state_row")
                report = compare_state(problem.expected_state, app.state(pod=name), problem.expected_events, receipts)
                if report["changed_records"] or report["duplicate_financial_effects"] or report["missing_events"]:
                    return self.fail("acknowledged_payment_state_changed", member=name, **report)
                if report["undelivered_events"]:
                    return self.fail("webhook_backlog_incomplete", member=name, **report)
            for payment in problem.payments:
                app.run_client("verify", **payment)
            result = super().evaluate()
            if not result.get("success"):
                return result
            source = (
                "import time,urllib.request\n"
                f"deadline=time.monotonic()+{self.stability_seconds}\nsamples=0\n"
                "while True:\n"
                " with urllib.request.urlopen('http://stripe-marathon:8000/v1/health',timeout=5) as r: assert r.status==200\n"
                " samples+=1\n"
                " if time.monotonic()>=deadline: break\n"
                " time.sleep(2)\nprint(samples)\n"
            )
            samples = int(
                app.command(
                    "exec",
                    "-i",
                    "application-client",
                    "--",
                    "python",
                    "-",
                    input_text=source,
                    timeout=self.stability_seconds + 30,
                )
            )
            if not app.configuration_safety()["safe"]:
                return self.fail("traffic_unstable")
            return {
                **result,
                "acknowledged_payments_verified": len(problem.payments),
                "original_webhooks_delivered": len(problem.expected_events),
                "configuration": safety,
                "stable_traffic_seconds": self.stability_seconds,
                "traffic_samples": samples,
            }
        except Exception as exc:
            return self.fail_from_exception(exc)
