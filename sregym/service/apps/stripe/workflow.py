"""Stripe seed, functional probe, and durability checks."""

import json
import time
import urllib.request


def workflow(c, mode, token=None, wait_webhook=True, webhook_url="http://stripe-receiver:8080/hook", **baseline):
    c.headers["Authorization"] = "Bearer " + c.token
    if mode == "seed":
        endpoint = c.call(
            "POST",
            "/v1/webhook_endpoints",
            {"url": webhook_url, "enabled_events": ["payment_intent.succeeded"]},
        )
        return {"endpoint": endpoint["id"]}
    if mode == "probe":
        idem = {"Idempotency-Key": "customer-" + token}
        params = {"email": token + "@example.test", "metadata": {"probe": token}}
        customer = c.call("POST", "/v1/customers", params, idem)
        c.require(
            c.call("POST", "/v1/customers", params, idem)["id"] == customer["id"],
            "Idempotency created duplicate customers",
        )
        pm = c.call(
            "POST",
            "/v1/payment_methods",
            {"type": "card", "card": {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"}},
        )
        payment_params = {
            "amount": 2500,
            "currency": "usd",
            "customer": customer["id"],
            "payment_method": pm["id"],
            "confirm": True,
        }
        pi = c.call("POST", "/v1/payment_intents", payment_params, {"Idempotency-Key": "payment-" + token})
        c.require(pi["status"] == "succeeded", "Payment was not captured")
        refund = c.call(
            "POST", "/v1/refunds", {"payment_intent": pi["id"], "amount": 100}, {"Idempotency-Key": "refund-" + token}
        )
        result = {
            "token": token,
            "customer": customer["id"],
            "payment": pi["id"],
            "charge": pi["latest_charge"],
            "refund": refund["id"],
            "payment_params": payment_params,
        }
        if wait_webhook:
            workflow(c, "verify", **result)
        return result
    customer = c.call("GET", "/v1/customers/" + baseline["customer"])
    c.require(customer["email"] == token + "@example.test", "Acknowledged customer changed")
    pi = c.call("GET", "/v1/payment_intents/" + baseline["payment"])
    # The pinned reference subtracts refunds from amount_received.
    c.require(
        pi["status"] == "succeeded"
        and pi["amount"] == 2500
        and pi["currency"] == "usd"
        and pi["amount_received"] == 2400
        and pi["latest_charge"] == baseline["charge"],
        "Acknowledged payment changed",
    )
    charge = c.call("GET", "/v1/charges/" + baseline["charge"])
    c.require(charge["paid"] and charge["amount_captured"] == 2500, "Captured charge changed")
    refunds = charge["refunds"]["data"]
    c.require(
        charge["amount_refunded"] == 100
        and len(refunds) == 1
        and refunds[0]["id"] == baseline["refund"]
        and refunds[0]["amount"] == 100
        and refunds[0]["status"] == "succeeded",
        "Refund was lost or duplicated",
    )
    replay = c.call("POST", "/v1/payment_intents", baseline["payment_params"], {"Idempotency-Key": "payment-" + token})
    c.require(replay["id"] == pi["id"], "Restart lost idempotency and created another payment")
    deadline = time.monotonic() + 45
    while True:
        with urllib.request.urlopen("http://stripe-receiver:8080/receipts", timeout=5) as response:
            receipts = json.load(response)
        if any(x["data"]["object"]["id"] == pi["id"] for x in receipts if x["type"] == "payment_intent.succeeded"):
            break
        c.require(time.monotonic() < deadline, "Acknowledged payment webhook did not drain")
        time.sleep(1)
    return {"verified": True}
