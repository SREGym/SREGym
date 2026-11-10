"""Mitigation oracle for n8n_payment_batch_remittance_files_lost.

The background ERP stops submitting batches. Every check runs and is reported,
also after an earlier one failed; the verdict is the first failing check in this
order:

1. Damage (repaired; checked after the probe so that batches resubmitted
   shortly before the evaluation have passed their hold): every payment batch the ERP submitted before the
   evaluation (and at least ``GRACE_S`` earlier, so it had time to pass its
   cut-off) is settled in payables-portal, each of its lines has exactly one live
   payout at payments-gateway with the line's amount and currency, and each
   supplier got a remittance advice for the line (an advice that names the line
   and its amount). A duplicate payout is harm, not a repair.
2. Service: n8n-main and both workers ready and "Supplier payment batch"
   published.
3. Recurrence (as treasury would see it): the ERP submits one batch that pays
   EUR and foreign-currency suppliers together and one EUR-only batch. While
   their executions sit in the release hold, the oracle performs exactly what
   n8n-main's pruning hard-delete cycle (every 15 minutes) does: it deletes the
   soft-deleted executions together with their binary data (``hardDelete``:
   ``binary_data`` rows of those executions, then the execution rows). Both
   batches must then be settled, paid once per line and advised within
   ``PROBE_TIMEOUT_S``. So the probe does not depend on where the next pruning
   tick falls. The unfixed workflows fail the mixed batch again; a retry of the
   failed executions, a restart, or turning pruning off (an unsaved child is
   then hard-deleted at once) does not change that. Any repair that keeps the
   advice files the parent still needs passes: a sub-workflow trigger that
   returns its output with the files copied to the parent (Execute Workflow
   Trigger 1.3), one run of the file node, a saved child, or reading the files
   before the hold.

``collateral`` lists harmless but notable side effects (n8n pods restarted,
workflow versions published) without failing the attempt.
"""

from __future__ import annotations

import copy
import json
import logging
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

logger = logging.getLogger(__name__)


class N8nPaymentBatchOracle(Oracle):
    evaluation_timeout_seconds = 2400
    #: Batches submitted this shortly before the evaluation may still be waiting for their cut-off.
    GRACE_S = 60
    PROBE_TIMEOUT_S = 900
    #: The workflow's "Release hold".
    HOLD_S = 600
    SETTLE_MARGIN_S = 150

    FAILURE_CLASSES = {
        "n8n_not_ready": FailureClass.AGENT_ERROR,
        "payment_batch_not_published": FailureClass.AGENT_ERROR,
        "batches_not_completed": FailureClass.AGENT_ERROR,
        "duplicate_payouts": FailureClass.AGENT_ERROR,
        "probe_batch_not_completed": FailureClass.AGENT_ERROR,
        "traffic_missing": FailureClass.AMBIGUOUS,
        "probe_failed": FailureClass.AMBIGUOUS,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.baseline_pods: dict[str, str] = {}
        self.baseline_versions: dict[str, str] = {}

    def capture_baseline(self) -> None:
        try:
            self.baseline_pods = self._pods()
            self.baseline_versions = self._versions()
        except Exception:  # noqa: BLE001
            self.baseline_pods = {}

    def capture_handover(self) -> None:
        """State once the injection is complete: collateral is what changed after it."""
        self.capture_baseline()

    def _pods(self) -> dict[str, str]:
        pods = json.loads(self.problem.app.command("get", "pods", "-o", "json"))["items"]
        result = {}
        for pod in pods:
            restarts = sum(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", []))
            result[pod["metadata"]["name"]] = f"{pod['metadata']['uid']}:{restarts}"
        return result

    def _versions(self) -> dict[str, str]:
        rows = self.problem.app.psql('SELECT name, "versionId" || \' \' || "updatedAt"::text FROM workflow_entity')
        return {name: version for name, version in rows}

    def evaluate(self) -> dict:
        app = self.problem.app
        if app.traffic is None:
            return self.fail("traffic_missing")
        try:
            return self._evaluate(app, app.traffic)
        except Exception as error:
            logger.exception("n8n payment batch probe failed")
            print(f"[n8n oracle] probe failed: {error}")
            return self.fail("probe_failed", error=str(error))

    # ------------------------------------------------------------------ checks
    def _damage(self, traffic, batch_ids):
        problems = traffic.check(batch_ids)
        duplicates = {
            b: [i for i in issues if "live payouts" in i and not i.endswith(": 0 live payouts")]
            for b, issues in problems.items()
        }
        duplicates = {b: v for b, v in duplicates.items() if v}
        if duplicates:
            return self.fail("duplicate_payouts", batches=dict(list(duplicates.items())[:10]), count=len(duplicates))
        if problems:
            return self.fail("batches_not_completed", count=len(problems), sample=dict(list(problems.items())[:8]))
        return None

    def _health(self, app):
        for name in ("n8n-main", "n8n-worker"):
            try:
                app.rollout(name, timeout=300)
            except Exception as error:
                return self.fail("n8n_not_ready", deployment=name, error=str(error)[-500:])
        try:
            if not app.workflow().get("active"):
                return self.fail("payment_batch_not_published")
            app.wait_webhook()
        except Exception as error:
            return self.fail("n8n_not_ready", error=str(error)[-500:])
        return None

    def _probe(self, traffic):
        mixed = traffic.submit(traffic.build(2, 1))
        domestic = traffic.submit(traffic.build(2, 0))
        probe = [mixed["batch_id"], domestic["batch_id"]]
        print(f"[n8n oracle] probe batches {probe} (one with a foreign-currency supplier)")
        deadline = time.time() + self.PROBE_TIMEOUT_S
        pruned = self._prune_when_held(traffic, probe, deadline)
        problems = traffic.check(probe)
        # a batch settles a few seconds after its hold ends; give it HOLD_S + SETTLE_MARGIN_S from submission
        deadline = min(deadline, max(mixed["submitted"], domestic["submitted"]) + self.HOLD_S + self.SETTLE_MARGIN_S)
        while time.time() < deadline and problems:
            time.sleep(15)
            problems = traffic.check(probe)
        detail = {
            "probe": probe,
            "submit_status": [mixed["submit_status"], domestic["submit_status"]],
            "pruned": pruned,
        }
        if problems:
            return self.fail("probe_batch_not_completed", problems=problems, **detail), detail
        return None, detail

    def _prune_when_held(self, traffic, probe, deadline) -> dict:
        """Once the probe batches are registered and their sub-workflow runs have ended, run n8n's hard-delete step."""
        app = self.problem.app
        held_since = None
        while time.time() < deadline:
            time.sleep(10)
            batches = traffic.portal_batches()
            if not all(b in batches for b in probe):
                continue
            busy = app.psql(
                "SELECT count(*) FROM execution_entity WHERE status IN ('new', 'running') AND \"deletedAt\" IS NULL"
            )
            if busy and busy[0][0] == "0":
                held_since = held_since or time.time()
                if time.time() - held_since >= 20:
                    break
            elif all((batches[b].get("status") == "settled") for b in probe):
                break
        rows = app.psql(
            'WITH gone AS (SELECT id FROM execution_entity WHERE "deletedAt" IS NOT NULL), '
            'files AS (DELETE FROM binary_data WHERE "sourceType" = \'execution\' AND "sourceId" IN '
            "(SELECT id::text FROM gone) RETURNING 1), "
            "execs AS (DELETE FROM execution_entity WHERE id IN (SELECT id FROM gone) RETURNING 1) "
            "SELECT (SELECT count(*) FROM files), (SELECT count(*) FROM execs)"
        )
        result = {"binary_files": int(rows[0][0]), "executions": int(rows[0][1])} if rows else {}
        print(f"[n8n oracle] pruning hard-delete step while the probe batches are held: {result}")
        return result

    def _collateral(self) -> dict:
        result = {}
        try:
            now = self._pods()
            result["pods_replaced_or_restarted"] = sorted(
                name
                for name, state in self.baseline_pods.items()
                if now.get(name) != state and name.startswith(("n8n-", "postgres", "redis"))
            )
        except Exception as error:  # noqa: BLE001
            result["pods_error"] = str(error)
        try:
            versions = self._versions()
            result["workflows_changed"] = sorted(n for n, v in versions.items() if self.baseline_versions.get(n) != v)
        except Exception as error:  # noqa: BLE001
            result["workflows_error"] = str(error)
        return result

    # ------------------------------------------------------------------ verdict
    def _evaluate(self, app, traffic) -> dict:
        started = time.time()
        traffic.quiesce()
        traffic.start()
        with traffic.lock:
            before = [b for b, v in traffic.batches.items() if v["submitted"] < started - self.GRACE_S]
        print(f"[n8n oracle] background batches stopped; checking {len(before)} earlier batches")
        checks = {"service": self._health(app)}
        if checks["service"] is None:
            checks["recurrence"], probe = self._probe(traffic)
        else:
            checks["recurrence"], probe = self.fail("probe_failed", reason="service not ready"), None
        # after the probe, so batches the agent resubmitted shortly before have passed their hold
        checks["damage"] = self._damage(traffic, before)
        collateral = self._collateral()
        report = copy.deepcopy({name: (verdict or {"success": True}) for name, verdict in checks.items()})
        print(f"[n8n oracle] checks {report}; collateral {collateral}")
        for name in ("damage", "service", "recurrence"):
            if checks[name] is not None:
                verdict = copy.deepcopy(checks[name])
                verdict.setdefault("detail", {})
                verdict["detail"]["checks"] = report
                verdict["collateral"] = collateral
                return verdict
        return {"success": True, "checks": report, "probe": probe, "collateral": collateral}
