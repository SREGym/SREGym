"""n8n: a PostgreSQL crash loses the worker's final save, so paid invoices show as crashed runs.

Upstream defect (n8n 2.43.5 = n8n-io/n8n@d811f5e8, unreported; found by model
checking, Specula MC-11, reproduced on the real binaries in queue mode): a
queue-mode worker persists a finished execution with one
``updateExistingExecution`` call (``hookFunctionsSaveWorker``,
execution-lifecycle-hooks.ts) and no retry. If it throws, the engine's catch
(workflow-execute.ts ``processRunExecutionData``) re-runs the
``workflowExecuteAfter`` hooks once with the database error as the run's error
and only ``console.error``s a second failure. The worker still logs ``Worker
finished execution N`` and reports the job finished, Bull completes the job, and
n8n-main finalizes from that message without writing the status. So when
PostgreSQL is unreachable at the moment a run finishes, the row of a run that
succeeded stays ``running`` with no run data until queue recovery (on the
leader at start-up, then every 180 min) marks it ``crashed``. n8n's Retry then
accepts it (``finished`` is false) and its side effects run again.

Here: "Supplier invoice payment" (webhook -> "Schedule payout" at
payments-gateway -> "Bank confirmation", a 3 s in-process wait -> "Payout
status" -> "Mark invoice paid" in payables-portal). The ERP posts each invoice
an AP clerk approves; it re-sends posts that fail.

Trigger (deterministic driver, natural state): after a healthy lead-in an AP
clerk approves a batch of invoices at once. When their executions have
scheduled their payouts and sit in "Bank confirmation", PostgreSQL stalls for
a few seconds the way a process at its memory limit does (the conductor
SIGSTOPs the container's processes from the kind node; a readiness probe may
time out), the runs mark their invoices paid and block in their final save,
and then the kernel OOM-kills PostgreSQL (``memory.max`` lowered, then
``cgroup.kill``; the pod shows ``OOMKilled`` and restarts its container with
the normal limit). The blocked saves fail with the dropped connection and the
engine's re-save fails with ECONNREFUSED, so those executions stay
``running``. The stall stays below n8n's 5 s database ping timeout. About
seven minutes later the on-call engineer restarts n8n (``kubectl rollout
restart`` of n8n-worker and n8n-main) as a precaution, and n8n-main's start-up
queue recovery marks the stale rows ``crashed``.
Nothing of the driver remains (no process, file, environment variable or
Kubernetes object; the cgroup edits die with the container). The incident ages
``propagation_duration_s`` before handover.

What an operator sees: PostgreSQL restarted (OOMKilled) about half an hour
earlier, n8n restarted about seven minutes after that (so the n8n pods' logs
from the crash, including the workers' ``There was a problem running hook
"workflowExecuteAfter"`` stderr lines, are gone; nothing above INFO remains
from the cause), n8n-main's INFO ``Marked executions as `crashed``` at its
start, and ``crashed`` "Supplier invoice payment" executions with no node data
after the trigger (they look interrupted before doing anything) whose
``startedAt`` is seconds before the PostgreSQL crash and whose ``stoppedAt`` is
n8n-main's start. payments-gateway's payouts and payables-portal's "paid"
marks (written by the workflow's last node) for those invoices, timestamped
within seconds of the crash, show that every one of those runs finished.

Reference mitigation: verify each crashed execution against payments-gateway
and payables-portal, and record those that completed as succeeded in n8n
(``status = 'success'``, ``finished = true``, ``stoppedAt`` set) without
re-running them, so that no Retry can pay them again. Invoices whose run
really failed in the outage (a job a worker took while PostgreSQL was down is
stored ``error`` or ``crashed`` without a payout, and the ERP got a 200 for its
post) are posted again (or their executions retried) so they are paid once
(none occurred in the validation runs once the stall was capped). Tested
alternative: retry the crashed executions and then cancel each invoice's older
payout, so that exactly one live payout remains and the portal's payout id
(rewritten by the retry's "Mark invoice paid") is live; cancelling the newer
payout instead leaves the portal pointing at a cancelled payout and fails. A
blind "retry the crashed executions" pays every affected invoice twice; doing
nothing leaves retryable crashed records of paid invoices.

The repair is a record correction (with a duplicate-payment trap), not a
workflow change.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time

from sregym.conductor.oracles.apps.n8n.diagnosis import MechanismGatedJudgeOracle
from sregym.conductor.oracles.apps.n8n.invoice_payment import N8nInvoicePaymentOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.n8n.invoice_payment import N8nInvoicePayment
from sregym.service.apps.n8n.traffic import http
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.sregym.problems.n8n")

ROOT_CAUSE = (
    "Upstream n8n 2.43.5 defect in how a queue-mode worker persists a finished execution (n8n-worker; "
    "packages/cli/src/execution-lifecycle/execution-lifecycle-hooks.ts hookFunctionsSaveWorker and "
    "packages/core/src/execution-engine/workflow-execute.ts, shipped as dist/execution-lifecycle/"
    "execution-lifecycle-hooks.js and n8n-core's workflow-execute.js). The worker writes a finished run's status "
    "and run data to PostgreSQL with a single updateExistingExecution call and no retry. PostgreSQL stalled for a few seconds "
    "while several 'Supplier invoice payment' executions of an AP batch were in their 3-second 'Bank confirmation' "
    "wait, and was then OOM-killed (its container restarted and was unreachable for some seconds). Those runs ran "
    "'Payout status' and 'Mark invoice paid': each scheduled its payout at payments-gateway and marked its invoice "
    "paid in payables-portal, i.e. the runs succeeded. Their final save then failed with the database connection "
    "error; the engine's catch re-ran the workflowExecuteAfter hooks once with the DB error as the run's error, "
    "that second save failed too (ECONNREFUSED) and was only printed to stderr ('There was a problem running hook "
    "\"workflowExecuteAfter\"'). The worker nevertheless logged 'Worker finished execution N' and reported the job "
    "finished, Bull completed the job, and n8n-main never writes the status itself, so the execution rows stayed "
    "'running' with no run data after PostgreSQL came back. When n8n (workers and n8n-main) was restarted about seven "
    "minutes later, n8n-main's start-up queue recovery (recoverFromQueue; INFO 'Completed queue recovery check, recovered dangling "
    "executions') found these 'running' rows absent from the Bull queue and marked them 'crashed', still without "
    "run data. They look like runs interrupted before doing anything, but each one paid its invoice; n8n's Retry "
    "accepts them (finished is false) and would pay the invoices a second time. The PostgreSQL OOM kill is the "
    "trigger and the n8n restart only surfaced the stale rows; neither interrupted the runs. "
    "Grading requirements. Localization (D1): accept n8n as the faulty component, named as n8n-worker (its saving "
    "of finished executions), n8n's execution persistence, or n8n in general. PostgreSQL's crash may be named "
    "first as the trigger or listed among the involved components; fail D1-Q3 only if the wrong records are "
    "attributed to PostgreSQL, the n8n restart, the workers crashing, Redis, payments-gateway or payables-portal "
    "instead of n8n failing to record runs that had completed. Required core mechanism (needed to pass; answer "
    "D2-Q1 Yes only if both are present): (a) the affected executions actually ran to completion: their payouts "
    "were scheduled and their invoices marked paid, although n8n shows them crashed with no node data; and (b) n8n "
    "lost their completion record: the worker's write of the finished run (final status/result save, the "
    "workflowExecuteAfter hook) to PostgreSQL failed because the database was down at that moment, and nothing "
    "retried or reconciled it, so the rows stayed 'running' and were later marked 'crashed' (by queue recovery at "
    "the n8n restart). Accept observable equivalents of (b), for example 'n8n finished the runs but could not "
    "save their result during the outage and never retried, so the records were left unfinished' or 'the end-of-run "
    "update failed with ECONNREFUSED and was dropped'. Deeper detail that earns credit but is not required (D2-Q2: "
    "answer Yes if at least two are given), such as: a single updateExistingExecution call without retry; the "
    "engine's re-save failing too (the stderr 'There was a problem running hook \"workflowExecuteAfter\"'); the "
    "worker reporting the job finished and Bull completing it while main does not write the status; "
    "recoverFromQueue at main's start-up marking running rows that are not in the queue as crashed; Retry being "
    "accepted because finished is false; the affected execution or invoice ids. WRONG mechanisms the evidence "
    "invites; a diagnosis that states one of them as the cause must get No for D2-Q1, D2-Q2, D2-Q3 and D3-Q3, even "
    "if it lists the affected executions correctly: (1) the PostgreSQL crash interrupted or killed "
    "the executions mid-run, so their payments did not happen, are incomplete or unknown, and they need to be "
    "retried or re-sent; (2) the restart of n8n (n8n-main or the workers) killed or crashed running executions; (3) the workers crashed, ran "
    "out of memory or lost the jobs, or Redis lost them; (4) payments-gateway or payables-portal failed; (5) the "
    "'Bank confirmation' wait lost or suspended the executions; (6) only 'database errors during the outage' "
    "without stating that the runs completed (paid) while n8n recorded them as unfinished/crashed; (7) the runs "
    "completed and were recorded, and n8n-main's queue recovery at the restart wrongly overwrote finished "
    "executions with 'crashed' (it did not: their rows were still 'running' because the final save had been "
    "lost); a diagnosis with (a) that blames the recovery or the restart for the wrong status, without the lost "
    "final save, gets No for D2-Q1. Saying that the "
    "executions completed and their records are wrong, with the save failure as the reason, is correct even if the "
    "diagnosis does not name the source functions. Executions whose job a worker took while PostgreSQL was down "
    "may be stored 'error' (a connection error) or 'crashed' without any payout; those really did not pay, and "
    "saying so (and retrying them) is correct, not a wrong mechanism."
)


class N8nInvoicePaymentIncident(Problem):
    LEAD_IN_S = 600
    MIN_PAID_BEFORE = 6
    BATCH = 7
    MIN_AFFECTED = 3
    ATTEMPTS = 3
    #: Seconds after the PostgreSQL crash at which the on-call engineer restarts n8n-main.
    MAIN_RESTART_AFTER_S = 420
    #: Longest stall before the OOM kill; runs still in their wait then finish while PostgreSQL is down.
    MAX_STALL_S = 3.4
    #: memory.max written into the postgres container's cgroup so that the kernel OOM-kills it.
    OOM_LIMIT = "1M"

    def __init__(self):
        super().__init__(app=N8nInvoicePayment())
        self.kubectl = self.app.kubectl
        self.faulty_service = "n8n-worker"
        self.propagation_duration_s = 1200
        self.injected_at = None
        self.affected: dict[str, dict] = {}
        self.trigger: dict = {}
        self.root_cause = self.build_structured_root_cause(
            component="deployment/n8n-worker", namespace=self.namespace, description=ROOT_CAUSE
        )
        self.diagnosis_oracle = MechanismGatedJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = N8nInvoicePaymentOracle(problem=self)

    @property
    def traffic(self):
        if self.app.traffic is None:
            raise RuntimeError("The invoice traffic is not running; start the workload first")
        return self.app.traffic

    # ------------------------------------------------------------------ node-level helpers
    @staticmethod
    def _node(container, script, timeout=60):
        return subprocess.run(
            ["docker", "exec", container, "sh", "-c", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout

    def _postgres_cgroup(self) -> tuple[str, str]:
        """(kind node container, cgroup directory) of the postgres container."""
        pod = json.loads(self.app.command("get", "pods", "-l", "app=postgres", "-o", "json"))["items"][0]
        provider = json.loads(self.app.command("get", "node", pod["spec"]["nodeName"], "-o", "json"))["spec"].get(
            "providerID", ""
        )
        if not provider.startswith("kind://"):
            raise RuntimeError("This problem drives the PostgreSQL crash from its node and supports kind clusters")
        container = provider.rsplit("/", 1)[-1]
        name = pod["metadata"]["name"]
        script = (
            f"cid=$(crictl ps -q --label io.kubernetes.pod.name={name} --label io.kubernetes.container.name=postgres) "
            "&& pid=$(crictl inspect -o go-template --template '{{.info.pid}}' $cid) && cut -d: -f3 /proc/$pid/cgroup"
        )
        cgroup = self._node(container, script).strip()
        if not cgroup.startswith("/"):
            raise RuntimeError(f"Could not find the cgroup of postgres in {name} on {container}: {cgroup!r}")
        return container, f"/sys/fs/cgroup{cgroup}"

    # ------------------------------------------------------------------ observation
    def _execution_states(self, since: float) -> dict[str, dict]:
        """Executions of the payment workflow created since ``since``: id -> {status, invoice}."""
        workflow_id = self.app.find_workflow_id(self.app.WORKFLOW_NAME)
        rows = self.app.psql(
            'SELECT e.id, e.status, d.data FROM execution_entity e JOIN execution_data d ON d."executionId" = e.id '
            f'WHERE e."workflowId" = \'{workflow_id}\' AND e."createdAt" >= to_timestamp({since:.3f}) ORDER BY e.id'
        )
        result = {}
        for row in rows:
            invoice = None
            for candidate in self.traffic.approved:
                if f'"{candidate}"' in row[2]:
                    invoice = candidate
                    break
            result[row[0]] = {"status": row[1], "invoice": invoice}
        return result

    def _paid(self, invoices) -> set[str]:
        rows = {r["id"]: r for r in self.app.portal("/api/invoices?status=paid&limit=500")}
        return {i for i in invoices if i in rows}

    def _wait_postgres(self, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.app.psql("SELECT 1") == [["1"]]:
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)
        raise RuntimeError("PostgreSQL did not come back")

    # ------------------------------------------------------------------ injection
    def _batch_payouts(self, batch, since_iso) -> dict[str, str]:
        """Payouts the gateway scheduled for the batch so far: invoice -> payout id."""
        status, payouts = http(
            "GET", f"{self.app.endpoints()['gateway']}/v1/payouts?since={since_iso}&limit=1000", timeout=10
        )
        if status != 200 or not isinstance(payouts, list):
            return {}
        return {p["reference"]: p["id"] for p in payouts if p["reference"] in batch}

    def _attempt(self) -> dict | None:
        traffic = self.traffic
        container, cgroup = self._postgres_cgroup()
        since = time.time() - 1
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since))
        batch = set(traffic.batch(self.BATCH))
        print(f"[inject] AP batch approved and posted: {sorted(batch)}", flush=True)
        # Each run schedules its payout and then sits in the 3 s "Bank confirmation" wait.
        paying: dict[str, str] = {}
        first_seen = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            paying = self._batch_payouts(batch, since_iso)
            if len(paying) >= self.MIN_AFFECTED:
                first_seen = first_seen or time.monotonic()
                if len(paying) == len(batch) or time.monotonic() - first_seen > 0.6:
                    break
            time.sleep(0.1)
        if len(paying) < self.MIN_AFFECTED:
            print(f"[inject] only {len(paying)} runs of the batch reached the payout; next batch later", flush=True)
            return None
        # PostgreSQL stalls (as at its memory limit), the runs finish and block in their final save,
        # then the kernel OOM-kills it.
        self._node(container, f"for p in $(cat {cgroup}/cgroup.procs); do kill -STOP $p; done")
        stalled_at = time.monotonic()
        invoices = set(paying)
        # Kill well before n8n's 5 s ping timeout: a ping that times out against the stalled server
        # can leave a worker's pool unusable after PostgreSQL is back, a different failure.
        while time.monotonic() - stalled_at < self.MAX_STALL_S - 0.3 and self._paid(invoices) != invoices:
            time.sleep(0.1)
        time.sleep(max(0.0, min(0.3, self.MAX_STALL_S - (time.monotonic() - stalled_at))))
        self._node(container, f"echo {self.OOM_LIMIT} > {cgroup}/memory.max; echo 1 > {cgroup}/cgroup.kill", timeout=30)
        self.injected_at = time.time()
        paid = self._paid(invoices)
        print(
            f"[inject] postgres OOM-killed at {self.injected_at:.1f} after a {time.monotonic() - stalled_at:.1f}s stall; "
            f"{len(paid)}/{len(invoices)} paying runs had marked their invoice paid",
            flush=True,
        )
        self._wait_postgres()
        time.sleep(10)
        states = self._execution_states(since)
        stuck = {e: s for e, s in states.items() if s["status"] in ("new", "running")}
        payouts = traffic.live_payouts()
        affected = {e: s for e, s in stuck.items() if s["invoice"] in payouts}
        print(f"[inject] after the restart: {json.dumps(states)}")
        if len(affected) < self.MIN_AFFECTED:
            print(f"[inject] only {len(affected)} paid executions left unfinished; next batch later")
            return None
        return {
            "batch": sorted(batch),
            "killed_at": self.injected_at,
            "stuck": stuck,
            "affected": affected,
        }

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: an AP batch meets a PostgreSQL crash ==")
        traffic = self.traffic
        started = time.time()
        deadline = time.monotonic() + self.LEAD_IN_S + 900
        while time.monotonic() < deadline:
            time.sleep(15)
            paid = traffic.live_payouts()
            if time.time() - started >= self.LEAD_IN_S and len(paid) >= self.MIN_PAID_BEFORE:
                break
        else:
            raise RuntimeError(f"No healthy lead-in: {traffic.summary()}")
        if traffic.duplicates():
            raise RuntimeError("Duplicate payouts exist before injection")
        for _attempt in range(self.ATTEMPTS):
            self.trigger = self._attempt() or {}
            if self.trigger:
                break
            time.sleep(240)
        else:
            raise RuntimeError("Could not time the PostgreSQL crash with finishing payment runs")
        wait = self.injected_at + self.MAIN_RESTART_AFTER_S - time.time()
        if wait > 0:
            time.sleep(wait)
        self.app.command("rollout", "restart", "deployment/n8n-worker", "deployment/n8n-main")
        self.app.rollout("n8n-worker")
        self.app.rollout("n8n-main")
        self.app.wait_webhook()
        time.sleep(20)
        ids = ",".join(f"'{e}'" for e in self.trigger["affected"])
        rows = self.app.psql(f"SELECT id, status FROM execution_entity WHERE id IN ({ids})")
        self.affected = {
            eid: {"invoice": self.trigger["affected"][eid]["invoice"], "status": status} for eid, status in rows
        }
        crashed = [e for e, a in self.affected.items() if a["status"] == "crashed"]
        if len(crashed) < self.MIN_AFFECTED:
            raise RuntimeError(f"The restarted n8n-main did not mark the stale executions crashed: {self.affected}")
        logger.info("n8n injection: trigger=%s affected=%s", json.dumps(self.trigger), json.dumps(self.affected))
        self.mitigation_oracle.capture_handover()
        print(f"[inject] {len(crashed)} paid invoices left with crashed executions: {json.dumps(self.affected)}")

    # ------------------------------------------------------------------ reference mitigation and recovery
    def completed_crashed(self) -> dict[str, str]:
        """Crashed or unfinished executions whose invoice has a live payout and is paid: id -> invoice."""
        workflow_id = self.app.find_workflow_id(self.app.WORKFLOW_NAME)
        rows = self.app.psql(
            'SELECT e.id, d.data FROM execution_entity e JOIN execution_data d ON d."executionId" = e.id '
            f"WHERE e.\"workflowId\" = '{workflow_id}' AND e.status IN ('crashed', 'running', 'new') "
            'AND e."retrySuccessId" IS NULL AND e."createdAt" < now() - interval \'5 minutes\''
        )
        payouts = self.traffic.live_payouts()
        paid = {r["id"]: r for r in self.app.portal("/api/invoices?status=paid&limit=2000")}
        result = {}
        for eid, data in rows:
            invoice = next((i for i in payouts if f'"{i}"' in data), None)
            if invoice and invoice in paid and paid[invoice]["payout_id"] in {p["id"] for p in payouts[invoice]}:
                result[eid] = invoice
        return result

    def reference_mitigation(self):
        """Record every crashed execution whose invoice was paid as the success it was; re-run nothing.

        If an invoice has more than one live payout (only after someone retried), the payout the
        portal's paid invoice does not point at is cancelled as well.
        """
        cancelled = []
        paid = {r["id"]: r for r in self.app.portal("/api/invoices?status=paid&limit=5000")}
        for invoice, payouts in self.traffic.duplicates().items():
            keep = (paid.get(invoice) or {}).get("payout_id") or payouts[0]["id"]
            for payout in payouts:
                if payout["id"] != keep and payout["status"] == "scheduled":
                    cancelled.append((payout["id"], self.app.cancel_payout(payout["id"], "duplicate payment")[0]))
        done = self.completed_crashed()
        if done:
            ids = ",".join(f"'{e}'" for e in done)
            self.app.psql(
                "UPDATE execution_entity SET status = 'success', finished = true, "
                f'"stoppedAt" = COALESCE("stoppedAt", now()) WHERE id IN ({ids})'
            )
        retried = self.retry_unpaid()
        return {"recorded_success": done, "cancelled": cancelled, "retried_unpaid": retried}

    def retry_unpaid(self, older_than_s: float = 300) -> list:
        """Re-send approved invoices that have no payout at all (runs that really did not pay), as the ERP would."""
        portal = {r["id"]: r for r in self.app.portal("/api/invoices?status=approved&limit=5000")}
        resent = []
        for invoice in self.traffic.unpaid(older_than_s):
            if invoice in portal:
                resent.append((invoice, self.traffic.post(invoice)))
        deadline = time.monotonic() + 90
        while resent and time.monotonic() < deadline:
            live = self.traffic.live_payouts()
            if all(invoice in live for invoice, _ in resent):
                break
            time.sleep(5)
        return resent

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: completed executions recorded as succeeded ==")
        if self.app.traffic is None:
            return
        try:
            self.reference_mitigation()
        except Exception as error:  # the namespace may already be gone
            print(f"[recover] reference mitigation failed: {error}")
