"""Accounts payable and the ERP for the supplier invoice payments, outside the cluster.

Runs in the conductor process and reaches the payables portal, n8n's production
webhook and the payments gateway through their NodePorts. Nothing here is
shipped into the cluster.

- An AP clerk approves a supplier invoice for payment every 20-45 s in the
  portal; the ERP then posts it to n8n's production webhook
  /webhook/invoice-payments. The ERP's outbound queue re-sends a post that did
  not get a 2xx answer (connection error or 5xx) after 20-40 s, up to ten times.
- ``batch`` is the AP clerk approving several invoices at once ("pay selected"),
  which the ERP posts together.

``approved`` (invoice id -> invoice and its webhook posts) is the ground truth
of what must be paid exactly once.
"""

from __future__ import annotations

import itertools
import logging
import random
import threading
import time

from sregym.service.apps.n8n.traffic import APPROVERS_AP, VENDORS, http

logger = logging.getLogger("all.sregym.n8n.invoice_payment")

RETRY_S = (20, 40)
MAX_POSTS = 10


class InvoicePaymentTraffic:
    def __init__(self, endpoints, seed: int | None = None):
        self.endpoints = endpoints
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.approved: dict[str, dict] = {}
        self.background = True
        self.errors = 0
        self._outbox: list[tuple[float, str]] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seq = itertools.count(int(time.time() / 7) % 90000 + 10000)

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._threads:
            return
        self._stop.clear()
        for target in (self._approve_loop, self._outbox_loop):
            thread = threading.Thread(target=self._guard(target), name=f"invoices-{target.__name__}", daemon=True)
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

    def resume(self):
        with self.lock:
            self.background = True

    def _guard(self, loop):
        def run():
            while not self._stop.is_set():
                try:
                    loop()
                    return
                except Exception:
                    self.errors += 1
                    logger.exception("invoice traffic loop %s failed; restarting", loop.__name__)
                    self._stop.wait(5)
                    try:
                        self.endpoints(refresh=True)
                    except Exception:
                        logger.warning("could not resolve the endpoints again", exc_info=True)

        return run

    # ------------------------------------------------------------------ AP and ERP
    def approve(self, amount: float | None = None) -> dict:
        vendor, iban = self.rng.choice(VENDORS)
        invoice = {
            "id": f"INV-{next(self._seq)}",
            "vendor": vendor,
            "iban": iban,
            "amount": f"{amount if amount is not None else round(self.rng.uniform(250, 9500), 2):.2f}",
            "currency": "EUR",
            "approved_by": self.rng.choice(APPROVERS_AP),
        }
        status, payload = http("POST", f"{self.endpoints()['portal']}/api/invoices", invoice)
        if status != 201:
            raise RuntimeError(f"portal answered {status} to invoice {invoice['id']}: {payload}")
        with self.lock:
            self.approved[invoice["id"]] = {**invoice, "approved_at": time.time(), "posts": []}
        return self.approved[invoice["id"]]

    def post(self, invoice_id: str) -> int | None:
        """The ERP posts an approved invoice to n8n; a failed post goes back into the outbox."""
        with self.lock:
            invoice = dict(self.approved[invoice_id])
        fields = ("id", "vendor", "iban", "amount", "currency", "approved_by")
        try:
            status, _ = http(
                "POST", f"{self.endpoints()['n8n']}/webhook/invoice-payments", {k: invoice[k] for k in fields}
            )
        except Exception as error:  # connection refused or reset while n8n restarts
            status = None
            logger.info("webhook post of %s failed: %s", invoice_id, error)
        with self.lock:
            record = self.approved[invoice_id]
            record["posts"].append({"at": time.time(), "status": status})
            if (status is None or status >= 500) and len(record["posts"]) < MAX_POSTS:
                self._outbox.append((time.time() + self.rng.uniform(*RETRY_S), invoice_id))
        return status

    def submit(self, amount: float | None = None) -> str:
        invoice = self.approve(amount)
        self.post(invoice["id"])
        return invoice["id"]

    def batch(self, count: int) -> list[str]:
        """Several invoices approved at once and posted by the ERP together."""
        ids = [self.approve()["id"] for _ in range(count)]
        threads = [threading.Thread(target=self.post, args=(i,), daemon=True) for i in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        return ids

    def _approve_loop(self):
        while not self._stop.wait(self.rng.uniform(20, 45)):
            if self.background:
                self.submit()

    def _outbox_loop(self):
        while not self._stop.wait(2):
            now = time.time()
            with self.lock:
                due = [i for at, i in self._outbox if at <= now]
                self._outbox = [(at, i) for at, i in self._outbox if at > now]
            for invoice_id in due:
                self.post(invoice_id)

    # ------------------------------------------------------------------ ground truth
    def payouts(self) -> list[dict]:
        status, payouts = http("GET", f"{self.endpoints()['gateway']}/v1/payouts?limit=100000")
        if status != 200 or not isinstance(payouts, list):
            raise RuntimeError(f"payments gateway answered {status}: {payouts}")
        return payouts

    def live_payouts(self, payouts: list[dict] | None = None) -> dict[str, list[dict]]:
        """Live (not cancelled) payouts per approved invoice, oldest first."""
        payouts = self.payouts() if payouts is None else payouts
        with self.lock:
            known = set(self.approved)
        by_reference: dict[str, list[dict]] = {}
        for payout in payouts:
            if payout["status"] != "cancelled" and payout["reference"] in known:
                by_reference.setdefault(payout["reference"], []).append(payout)
        return {r: sorted(items, key=lambda p: p["id"]) for r, items in by_reference.items()}

    def duplicates(self, payouts: list[dict] | None = None) -> dict[str, list[dict]]:
        return {r: items for r, items in self.live_payouts(payouts).items() if len(items) > 1}

    def unpaid(self, older_than_s: float = 600, payouts: list[dict] | None = None) -> list[str]:
        """Approved invoices older than ``older_than_s`` without a live payout and with no post still pending."""
        live = self.live_payouts(payouts)
        now = time.time()
        with self.lock:
            pending = {i for _, i in self._outbox}
            return sorted(
                i
                for i, invoice in self.approved.items()
                if now - invoice["approved_at"] > older_than_s and i not in live and i not in pending
            )

    def summary(self) -> dict:
        with self.lock:
            posts = [p for invoice in self.approved.values() for p in invoice["posts"]]
            return {
                "approved": len(self.approved),
                "posts": len(posts),
                "failed_posts": sum(1 for p in posts if p["status"] is None or p["status"] >= 500),
                "outbox": len(self._outbox),
                "errors": self.errors,
            }
