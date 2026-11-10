"""Treasury's ERP for the supplier payment batches, outside the cluster.

Runs in the conductor process and reaches n8n, the payables portal and the
payments gateway through their NodePorts. Nothing here is shipped into the
cluster.

- The ERP posts a payment batch of 2-5 supplier payments to n8n's production
  webhook /webhook/payment-batches every 70-130 s.
- Most suppliers are paid in EUR. ``cross_border_share`` is the share of batches
  that also pay suppliers invoiced in their own currency (GBP, SEK, NOK, CHF,
  USD); it is 0 until the month-end run (``month_end_batch``), which pays the
  foreign suppliers together with the EUR ones.
- ``quiesce`` stops new background batches.

``batches`` (batch id -> payload) is the ground truth of what must be paid
exactly once and advised to each supplier.
"""

from __future__ import annotations

import itertools
import logging
import random
import threading
import time

from sregym.service.apps.n8n.traffic import http

logger = logging.getLogger("all.sregym.n8n.payment_batch")

EUR_SUPPLIERS = (
    ("Nordwind Logistik GmbH", "DE44500105175407324931"),
    ("Atlas Packaging BV", "NL91ABNA0417164300"),
    ("Cobalt Steelworks SA", "FR1420041010050500013M02606"),
    ("Evergreen Pallets Oy", "FI2112345600000785"),
    ("Granite Machining SpA", "IT60X0542811101000000123456"),
    ("Helix Industrial Coatings SL", "ES9121000418450200051332"),
    ("Ironbark Fasteners GmbH", "AT611904300234573201"),
)
FOREIGN_SUPPLIERS = (
    ("Brightline Freight Ltd", "GB29NWBK60161331926819", "GBP"),
    ("Delta Cold Chain AB", "SE4550000000058398257466", "SEK"),
    ("Fjord Marine Supply AS", "NO9386011117947", "NOK"),
    ("Alpenland Tooling AG", "CH9300762011623852957", "CHF"),
    ("Keystone Sensors Inc", "GB33BUKB20201555555555", "USD"),
)


class PaymentBatchTraffic:
    def __init__(self, endpoints, seed: int | None = None):
        self.endpoints = endpoints
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.batches: dict[str, dict] = {}
        self.cross_border_share = 0.0
        self.background = True
        self.errors = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seq = itertools.count(int(time.time() / 11) % 9000 + 1000)
        self._invoice = itertools.count(int(time.time() / 3) % 90000 + 10000)

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._threads:
            return
        self._stop.clear()
        thread = threading.Thread(target=self._guard(self._erp_loop), name="payment-batch-erp", daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=30)
        self._threads = []

    def quiesce(self):
        with self.lock:
            self.background = False

    def _guard(self, loop):
        def run():
            while not self._stop.is_set():
                try:
                    loop()
                    return
                except Exception:
                    self.errors += 1
                    logger.exception("payment batch ERP loop failed; restarting")
                    self._stop.wait(5)
                    try:
                        self.endpoints(refresh=True)
                    except Exception:
                        logger.warning("could not resolve the endpoints again", exc_info=True)

        return run

    # ------------------------------------------------------------------ batches
    def _line(self, batch_id, index, supplier, currency):
        vendor, iban = supplier[0], supplier[1]
        invoices = ", ".join(f"INV-{next(self._invoice)}" for _ in range(self.rng.randint(1, 3)))
        return {
            "line_id": f"{batch_id}-{index:02d}",
            "vendor": vendor,
            "iban": iban,
            "amount": f"{round(self.rng.uniform(180, 9800), 2):.2f}",
            "currency": currency,
            "invoices": invoices,
        }

    def build(self, eur: int, foreign: int, prefix: str = "PB") -> dict:
        batch_id = f"{prefix}-{next(self._seq)}"
        suppliers = [(s, "EUR") for s in self.rng.sample(EUR_SUPPLIERS, eur)]
        suppliers += [(s, s[2]) for s in self.rng.sample(FOREIGN_SUPPLIERS, foreign)]
        self.rng.shuffle(suppliers)
        lines = [self._line(batch_id, i + 1, s, c) for i, (s, c) in enumerate(suppliers)]
        return {"batch_id": batch_id, "lines": lines}

    def submit(self, batch: dict, retries: int = 3) -> dict:
        record = {**batch, "submitted": time.time(), "submit_status": None}
        with self.lock:
            self.batches[batch["batch_id"]] = record
        for _attempt in range(retries + 1):
            try:
                status, _ = http("POST", f"{self.endpoints()['n8n']}/webhook/payment-batches", batch)
            except Exception as error:  # noqa: BLE001
                status = str(error)[:80]
            record["submit_status"] = status
            if status == 200:
                break
            time.sleep(5)
        return record

    def resubmit(self, batch_id: str) -> dict:
        with self.lock:
            batch = self.batches[batch_id]
        payload = {"batch_id": batch["batch_id"], "lines": batch["lines"]}
        status, _ = http("POST", f"{self.endpoints()['n8n']}/webhook/payment-batches", payload)
        batch.setdefault("resubmitted", []).append((time.time(), status))
        return {"batch_id": batch_id, "status": status}

    def next_batch(self) -> dict:
        foreign = 0
        if self.rng.random() < self.cross_border_share:
            foreign = self.rng.randint(1, 2)
        return self.build(self.rng.randint(2, 4), foreign)

    def month_end_batch(self) -> dict:
        """The month-end run pays the foreign suppliers together with EUR ones."""
        return self.submit(self.build(self.rng.randint(2, 3), self.rng.randint(2, 3)))

    def _erp_loop(self):
        while not self._stop.wait(self.rng.uniform(70, 130)):
            if self.background:
                self.submit(self.next_batch())

    # ------------------------------------------------------------------ business view
    def portal_batches(self) -> dict[str, dict]:
        status, rows = http("GET", f"{self.endpoints()['portal']}/api/payment-batches?limit=5000")
        if status != 200 or not isinstance(rows, list):
            raise RuntimeError(f"portal answered {status}: {rows}")
        return {r["id"]: r for r in rows}

    def remittances(self) -> dict[str, list[dict]]:
        status, rows = http("GET", f"{self.endpoints()['portal']}/api/remittances?limit=100000")
        if status != 200 or not isinstance(rows, list):
            raise RuntimeError(f"portal answered {status}: {rows}")
        result: dict[str, list[dict]] = {}
        for row in rows:
            result.setdefault(row["line_id"], []).append(row)
        return result

    def payouts(self) -> dict[str, list[dict]]:
        status, rows = http("GET", f"{self.endpoints()['gateway']}/v1/payouts?limit=100000")
        if status != 200 or not isinstance(rows, list):
            raise RuntimeError(f"payments gateway answered {status}: {rows}")
        result: dict[str, list[dict]] = {}
        for row in rows:
            if row["status"] != "cancelled":
                result.setdefault(row["reference"], []).append(row)
        return result

    def check(self, batch_ids=None) -> dict[str, list[str]]:
        """Problems per batch: lines not paid exactly once with the right amount, missing advices, not settled."""
        with self.lock:
            batches = {b: dict(v) for b, v in self.batches.items() if batch_ids is None or b in batch_ids}
        portal, remittances, payouts = self.portal_batches(), self.remittances(), self.payouts()
        problems: dict[str, list[str]] = {}
        for batch_id, batch in batches.items():
            issues = []
            if (portal.get(batch_id) or {}).get("status") != "settled":
                issues.append(f"not settled ({(portal.get(batch_id) or {}).get('status')})")
            for line in batch["lines"]:
                live = payouts.get(line["line_id"], [])
                if len(live) != 1:
                    issues.append(f"{line['line_id']}: {len(live)} live payouts")
                elif live[0]["amount"] != line["amount"] or live[0]["currency"] != line["currency"]:
                    issues.append(f"{line['line_id']}: payout {live[0]['amount']} {live[0]['currency']}")
                advices = remittances.get(line["line_id"], [])
                if not any(a["line_id"] in a["advice"] and line["amount"] in a["advice"] for a in advices):
                    issues.append(f"{line['line_id']}: no remittance advice")
            if issues:
                problems[batch_id] = issues
        return problems

    def summary(self) -> dict:
        with self.lock:
            return {
                "batches": len(self.batches),
                "cross_border_batches": sum(
                    1 for b in self.batches.values() if any(line["currency"] != "EUR" for line in b["lines"])
                ),
                "cross_border_share": self.cross_border_share,
                "errors": self.errors,
            }
