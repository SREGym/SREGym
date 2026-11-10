"""The n8n payables deployment running "Supplier payment batch" (see :mod:`.application`).

The published workflows are created from ``workflows/render_remittance_advices.json``
(the sub-workflow, created first) and ``workflows/supplier_payment_batch.json``,
whose Execute Workflow node names the sub-workflow as ``"@workflow:<name>"``;
that value is replaced by the sub-workflow's id at setup. payables-portal keeps
the payment batches and the remittance advices sent. The ERP's payment batches
come from :mod:`.payment_batch_traffic`, which runs in the conductor process.
"""

from __future__ import annotations

import json
import time

from sregym.service.apps.n8n.application import HERE, N8nPayables, N8nRest
from sregym.service.apps.n8n.payment_batch_traffic import PaymentBatchTraffic


class N8nPaymentBatch(N8nPayables):
    metadata_file = HERE / "metadata_payment_batch.json"
    WORKFLOW_NAME = "Supplier payment batch"
    CHILD_WORKFLOW = "Render remittance advices"
    WORKFLOW_FILES = (
        HERE / "workflows" / "render_remittance_advices.json",
        HERE / "workflows" / "supplier_payment_batch.json",
    )
    WEBHOOK_PATH = "payment-batches"
    TRAFFIC = PaymentBatchTraffic

    def setup_n8n(self):
        base = self.endpoints()["n8n"]
        self.wait_http(f"{base}/healthz/readiness", lambda status, body: status == 200)
        email, password = self.secret("n8n-owner", "email"), self.secret("n8n-owner", "password")
        rest = N8nRest(base)
        try:
            rest.login(email, password)
        except RuntimeError:
            rest.call(
                "POST",
                "/rest/owner/setup",
                {"email": email, "firstName": "Payables", "lastName": "Operations", "password": password},
            )
            rest.login(email, password)
        existing = {w["name"]: w["id"] for w in rest.call("GET", "/rest/workflows")}
        for path in self.WORKFLOW_FILES:
            definition = self.resolve_references(json.loads(path.read_text()))
            name = definition["name"]
            if name not in existing:
                created = rest.call("POST", "/rest/workflows", {**definition, "active": False})
                existing[name] = created["id"]
                rest.call("POST", f"/rest/workflows/{created['id']}/activate", {"versionId": created["versionId"]})
            self.workflow_ids[name] = existing[name]
        self.workflow_id = self.workflow_ids[self.WORKFLOW_NAME]
        self.wait_webhook()

    def resolve_references(self, definition):
        """Replace ``"@workflow:<name>"`` values by the id of that (already created) workflow."""
        raw = json.dumps(definition)
        for name, workflow_id in self.workflow_ids.items():
            raw = raw.replace(json.dumps(f"@workflow:{name}"), json.dumps(workflow_id))
        if '"@workflow:' in raw:
            raise RuntimeError(f"Unresolved sub-workflow reference in {definition['name']}")
        return json.loads(raw)

    def publish_workflow(self, nodes, connections, name: str | None = None):
        """Save a new version of a workflow (default: the main one) and publish it, as an editor user would."""
        rest = self.rest()
        current = self.workflow(rest, name)
        updated = rest.call(
            "PATCH",
            f"/rest/workflows/{current['id']}",
            {"nodes": nodes, "connections": connections, "versionId": current["versionId"]},
        )
        rest.call("POST", f"/rest/workflows/{current['id']}/activate", {"versionId": updated["versionId"]})
        self.wait_webhook()
        return updated

    def wait_workload_started(self):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            batches = list(self.traffic.batches.values())
            if any(b.get("submit_status") == 200 for b in batches):
                return
            time.sleep(5)
        raise RuntimeError(f"No payment batch reached n8n: {self.traffic.summary()}")
