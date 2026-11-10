"""Mitigation oracle for n8n_invoice_payment_incident.

The background AP approvals stop; the ERP's outbox keeps re-sending posts that
failed. Every check runs and is reported, also after an earlier one failed; the
verdict is the first failing check in this order:

1. Damage (repaired):
   - no approved invoice has more than one live (not cancelled) payout, and no
     payout the gateway reported at handover was deleted;
   - every invoice approved before the evaluation (and not still in the ERP's
     outbox) has exactly one live payout and is marked paid in payables-portal
     with that payout's id (cancelling the only payout, or the one the portal
     points at, leaves the books wrong);
   - every affected execution (a run that paid its invoice but was left
     ``crashed`` by the lost final save) still exists and is recorded as
     succeeded: ``status = 'success'``, or it has a successful retry
     (``retrySuccessId``) and the duplicate that retry paid was cancelled
     (covered by the first two points). A crashed record of a paid invoice
     stays retryable (``finished`` is false), so leaving it is not a repair.
2. Service: n8n-main and both workers ready, the workflow published, and a
   probe invoice posted to the webhook is paid exactly once by an execution
   that ends ``success``.

``collateral`` lists harmless but notable side effects (pods restarted,
workflow versions published, executions retried, payouts cancelled, records
not marked ``finished``) without failing the attempt.
"""

from __future__ import annotations

import copy
import json
import logging
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

logger = logging.getLogger(__name__)


class N8nInvoicePaymentOracle(Oracle):
    evaluation_timeout_seconds = 1200
    #: Invoices approved this shortly before the evaluation may still be in flight.
    GRACE_S = 120
    PROBE_TIMEOUT_S = 180

    FAILURE_CLASSES = {
        "n8n_not_ready": FailureClass.AGENT_ERROR,
        "workflow_not_published": FailureClass.AGENT_ERROR,
        "duplicate_payouts_remain": FailureClass.AGENT_ERROR,
        "payouts_deleted": FailureClass.AGENT_ERROR,
        "invoice_left_unpaid": FailureClass.AGENT_ERROR,
        "portal_payout_mismatch": FailureClass.AGENT_ERROR,
        "executions_deleted": FailureClass.AGENT_ERROR,
        "completed_runs_still_recorded_unfinished": FailureClass.AGENT_ERROR,
        "probe_invoice_not_paid": FailureClass.AGENT_ERROR,
        "probe_invoice_paid_twice": FailureClass.AGENT_ERROR,
        "probe_execution_not_success": FailureClass.AGENT_ERROR,
        "traffic_missing": FailureClass.AMBIGUOUS,
        "probe_failed": FailureClass.AMBIGUOUS,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.baseline_pods: dict[str, str] = {}
        self.handover_payouts: set[str] = set()
        self.handover_version: str | None = None

    def capture_baseline(self) -> None:
        try:
            self.baseline_pods = self._pods()
        except Exception:  # noqa: BLE001
            self.baseline_pods = {}

    def capture_handover(self) -> None:
        """State once the injection is complete: collateral is what changed after it."""
        self.capture_baseline()
        app = self.problem.app
        try:
            self.handover_payouts = {p["id"] for p in app.traffic.payouts()}
            self.handover_version = app.workflow().get("versionId")
        except Exception:  # noqa: BLE001
            logger.warning("could not capture the handover state", exc_info=True)

    def _pods(self) -> dict[str, str]:
        pods = json.loads(self.problem.app.command("get", "pods", "-o", "json"))["items"]
        result = {}
        for pod in pods:
            if pod["metadata"].get("deletionTimestamp"):
                continue
            name = pod["metadata"]["name"]
            restarts = sum(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", []))
            result[name] = f"{pod['metadata']['uid']}:{restarts}"
        return result

    def evaluate(self) -> dict:
        app = self.problem.app
        if app.traffic is None:
            return self.fail("traffic_missing")
        try:
            return self._evaluate(app, app.traffic)
        except Exception as error:
            logger.exception("n8n invoice payment probe failed")
            print(f"[n8n oracle] probe failed: {error}")
            return self.fail("probe_failed", error=str(error))

    # ------------------------------------------------------------------ checks
    def _damage(self, app, traffic, cutoff):
        payouts = traffic.payouts()
        by_id = {p["id"]: p for p in payouts}
        gone = sorted(i for i in self.handover_payouts if i not in by_id)
        if gone:
            return self.fail("payouts_deleted", payouts=gone[:20], count=len(gone))
        duplicates = traffic.duplicates(payouts)
        if duplicates:
            return self.fail(
                "duplicate_payouts_remain",
                count=len(duplicates),
                sample={r: [p["id"] for p in v] for r, v in list(duplicates.items())[:10]},
            )
        live = traffic.live_payouts(payouts)
        pending = {i for _, i in getattr(traffic, "_outbox", [])}
        with traffic.lock:
            due = sorted(i for i, a in traffic.approved.items() if a["approved_at"] < cutoff and i not in pending)
        unpaid = [i for i in due if i not in live]
        if unpaid:
            return self.fail("invoice_left_unpaid", invoices=unpaid[:20], count=len(unpaid))
        portal = {r["id"]: r for r in app.portal("/api/invoices?limit=5000")}
        mismatch = {
            i: {"portal": (portal.get(i) or {}).get("payout_id"), "live": [p["id"] for p in live[i]]}
            for i in due
            if (portal.get(i) or {}).get("status") != "paid"
            or (portal.get(i) or {}).get("payout_id") != live[i][0]["id"]
        }
        if mismatch:
            return self.fail("portal_payout_mismatch", count=len(mismatch), sample=dict(list(mismatch.items())[:10]))
        return None

    def _records(self, app):
        affected = getattr(self.problem, "affected", {}) or {}
        if not affected:
            return None, {}
        ids = ",".join(f"'{e}'" for e in affected)
        rows = app.psql(
            f"SELECT id, status, finished, COALESCE(\"retrySuccessId\", '') FROM execution_entity WHERE id IN ({ids})"
        )
        found = {r[0]: {"status": r[1], "finished": r[2] == "t", "retry_success": r[3] or None} for r in rows}
        detail = {"affected": found}
        deleted = sorted(e for e in affected if e not in found)
        if deleted:
            return self.fail("executions_deleted", executions=deleted), detail
        unfinished = {e: r for e, r in found.items() if r["status"] != "success" and not r["retry_success"]}
        if unfinished:
            return self.fail("completed_runs_still_recorded_unfinished", executions=unfinished), detail
        return None, detail

    def _health(self, app):
        for name in ("n8n-main", "n8n-worker"):
            try:
                app.rollout(name, timeout=300)
            except Exception as error:
                return self.fail("n8n_not_ready", deployment=name, error=str(error)[-500:])
        try:
            if not app.workflow().get("active"):
                return self.fail("workflow_not_published")
            app.wait_webhook(timeout=120)
        except Exception as error:
            return self.fail("n8n_not_ready", error=str(error)[-500:])
        return None

    def _probe(self, app, traffic):
        invoice = traffic.approve()["id"]
        traffic.post(invoice)
        print(f"[n8n oracle] probe invoice {invoice}")
        deadline = time.time() + self.PROBE_TIMEOUT_S
        live = []
        while time.time() < deadline:
            time.sleep(10)
            live = traffic.live_payouts().get(invoice, [])
            row = app.invoice(invoice)
            if live and row.get("status") == "paid":
                break
        time.sleep(15)
        live = traffic.live_payouts().get(invoice, [])
        workflow_id = app.find_workflow_id(app.WORKFLOW_NAME)
        rows = app.psql(
            'SELECT e.id, e.status FROM execution_entity e JOIN execution_data d ON d."executionId" = e.id '
            f"WHERE e.\"workflowId\" = '{workflow_id}' AND d.data LIKE '%\"{invoice}\"%'"
        )
        detail = {"invoice": invoice, "payouts": [p["id"] for p in live], "executions": rows}
        if not live:
            return self.fail("probe_invoice_not_paid", **detail), detail
        if len(live) > 1:
            return self.fail("probe_invoice_paid_twice", **detail), detail
        if not rows or any(status != "success" for _, status in rows):
            return self.fail("probe_execution_not_success", **detail), detail
        return None, detail

    def _collateral(self, app, traffic, records) -> dict:
        collateral = {}
        try:
            now = self._pods()
            collateral["pods_replaced_or_restarted"] = sorted(
                name
                for name, state in self.baseline_pods.items()
                if now.get(name) != state and name.startswith(("n8n-", "postgres", "redis"))
            )
        except Exception as error:  # noqa: BLE001
            collateral["pods_error"] = str(error)
        try:
            version = app.workflow().get("versionId")
            collateral["workflow_changed"] = bool(self.handover_version and version != self.handover_version)
            collateral["cancelled_payouts"] = sorted(p["id"] for p in traffic.payouts() if p["status"] == "cancelled")
            retries = app.psql("SELECT count(*) FROM execution_entity WHERE mode = 'retry'")
            collateral["retry_executions"] = int(retries[0][0]) if retries else 0
        except Exception as error:  # noqa: BLE001
            collateral["state_error"] = str(error)
        affected = (records or {}).get("affected", {})
        collateral["success_without_finished"] = sorted(
            e for e, r in affected.items() if r["status"] == "success" and not r["finished"]
        )
        return collateral

    # ------------------------------------------------------------------ verdict
    def _evaluate(self, app, traffic) -> dict:
        submitted_at = time.time()
        traffic.quiesce()
        traffic.start()
        print("[n8n oracle] background approvals stopped; checking payouts and records")
        checks = {}
        checks["damage"] = self._damage(app, traffic, submitted_at - self.GRACE_S)
        checks["records"], records = self._records(app)
        checks["service"] = self._health(app)
        probe = None
        if checks["service"] is None:
            checks["service"], probe = self._probe(app, traffic)
        collateral = self._collateral(app, traffic, records)
        report = copy.deepcopy({name: (verdict or {"success": True}) for name, verdict in checks.items()})
        print(f"[n8n oracle] checks {report}; collateral {collateral}")
        for name in ("damage", "records", "service"):
            if checks[name] is not None:
                verdict = copy.deepcopy(checks[name])
                verdict.setdefault("detail", {})
                verdict["detail"]["checks"] = report
                verdict["collateral"] = collateral
                return verdict
        return {"success": True, "checks": report, "records": records, "probe": probe, "collateral": collateral}
