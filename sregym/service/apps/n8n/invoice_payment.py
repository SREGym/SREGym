"""The n8n payables deployment running "Supplier invoice payment" (see :mod:`.application`).

The published workflow is created from ``workflows/supplier_invoice_payment.json``.
AP clerks' approvals and the ERP's webhook posts come from
:mod:`.invoice_payment_traffic`, which runs in the conductor process.
"""

from __future__ import annotations

import time

from sregym.service.apps.n8n.application import HERE, N8nPayables
from sregym.service.apps.n8n.invoice_payment_traffic import InvoicePaymentTraffic


class N8nInvoicePayment(N8nPayables):
    metadata_file = HERE / "metadata.json"
    WORKFLOW_NAME = "Supplier invoice payment"
    WORKFLOW_FILES = (HERE / "workflows" / "supplier_invoice_payment.json",)
    WEBHOOK_PATH = "invoice-payments"
    TRAFFIC = InvoicePaymentTraffic

    def wait_workload_started(self):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            invoices = list(self.traffic.approved.values())
            if any(p["status"] == 200 for invoice in invoices for p in invoice["posts"]):
                return
            time.sleep(5)
        raise RuntimeError(f"No invoice reached n8n: {self.traffic.summary()}")

    def invoice(self, invoice_id: str) -> dict:
        return self.portal(f"/api/invoices/{invoice_id}")
