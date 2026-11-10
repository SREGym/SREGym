"""n8n: a sub-workflow's files from an earlier run of its last node are not handed to the parent, so batches fail later.

Upstream defect (n8n 2.43.5 = n8n-io/n8n@d811f5e8, unreported; found by code
review, Specula CR-8, reproduced on the real binaries in queue and regular
mode): when a sub-workflow whose Execute Workflow Trigger is version 1.2 (or
1/1.1 with "All Items From Every Run") ends, ``buildSubWorkflowOutput`` returns
the items of *every* run of the child's last executed node to the parent
(``mergeRunsPerBranch``), but ``duplicateBinaryDataToParent``
(``execution-lifecycle-hooks``) copies binary data to the parent execution only
for the *last* run (all runs only under the 1.3 output policy). Items from the
earlier runs keep binary ids owned by the child execution. When the child is
deleted (here: save-on-success "none", so it is soft-deleted at its end and
n8n-main's pruning hard-delete cycle, every 15 minutes, removes it together
with its binary data), those items point to files that no longer exist. Nothing
above debug is logged at the cause.

Here: "Supplier payment batch" calls "Render remittance advices" (trigger 1.2,
save-on-success "none") with all payment lines. The child routes EUR lines
through "SEPA advice" and other currencies through "Cross-border advice"; both
feed "Advice document" (Convert to File), which therefore runs once per branch
when a batch has both kinds. The parent receives every line's advice item, but
only the cross-border files (the last run) are copied to the parent. After
treasury's 10-minute "Release hold", "Read advice" fails on the first EUR line
whose file the pruning cycle has deleted meanwhile (``Could not find any entity
of type "BinaryDataFile"``): no line of the batch is paid, no advice is sent,
the batch is not settled and payables-portal warns that the batch missed its
cut-off. EUR-only batches (one run of the file node) and mixed batches whose
hold ends before the next pruning tick are fine, so the failures look
intermittent.

Trigger (ordinary traffic, timed by the conductor): after a healthy lead-in of
EUR-only batches (with a routine payables-portal rollout at a quiet moment as
earlier pod churn), the ERP sends the month-end batch, which pays foreign
suppliers together with EUR ones, timed so that n8n-main's pruning tick
(main's start + k x 15 min; the conductor reads main's container start time)
falls inside its 10-minute hold. Afterwards a share of the ERP's batches pays
foreign suppliers too, some of which fail the same way. Nothing of the driver
remains in the cluster. The incident ages ``propagation_duration_s`` before
handover.

Reference mitigation: make the child hand all files to the parent (here: its
Execute Workflow Trigger upgraded to 1.3, whose output policy copies every
run's binary data), then resubmit every unsettled batch to the webhook (the
payouts carry the line id as Idempotency-Key, and a failed batch scheduled
nothing). Tested alternative: save the child's successful executions
(``saveDataSuccessExecution: all``), so its files are kept, and resubmit. Also
valid, black-box equivalent: one run of the file node (merge the branches
before it). A blind retry of the failed executions fails again (their stored
items still point to the deleted files); turning pruning off makes the unsaved
child hard-deleted at its end, which loses the files at once.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime

from sregym.conductor.oracles.apps.n8n.diagnosis import MechanismGatedJudgeOracle
from sregym.conductor.oracles.apps.n8n.payment_batch import N8nPaymentBatchOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.n8n.payment_batch import N8nPaymentBatch
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.sregym.problems.n8n")

ROOT_CAUSE = (
    "Upstream n8n 2.43.5 defect in how n8n hands a sub-workflow's binary data to its parent (n8n-worker running "
    "the executions; packages/cli/src/execution-lifecycle/execution-lifecycle-hooks.ts duplicateBinaryDataToParent "
    "and workflow-execute-additional-data.ts buildSubWorkflowOutput, shipped as dist/...js). 'Supplier payment "
    "batch' calls the sub-workflow 'Render remittance advices' with all payment lines (Execute Workflow, wait for "
    "completion). The child's trigger is Execute Workflow Trigger version 1.2 and it saves no successful "
    "executions (saveDataSuccessExecution 'none'). It routes EUR lines through 'SEPA advice' and other currencies "
    "through 'Cross-border advice'; both feed 'Advice document' (Convert to File), so in a batch with both kinds "
    "that node runs twice (first the SEPA run, then the cross-border run). n8n returns the items of every run of "
    "the child's last node to the parent, but copies the binary files to the parent execution only for the last "
    "run; the EUR advice items keep binary ids owned by the child execution. The unsaved child is soft-deleted at "
    "its end, and n8n-main's pruning hard-delete cycle (every 15 minutes) deletes it with its binary data. When "
    "the parent's 'Release hold' (10-minute wait) spans such a cycle, 'Read advice' fails after the hold on the "
    "first EUR line ('Could not find any entity of type \"BinaryDataFile\"'), so no line of the batch is paid, no "
    "advice is sent and the batch stays 'scheduled' (payables-portal warns that it missed its cut-off). In the "
    "binary_data table the surviving advice files of such a batch (the cross-border ones) are owned by the parent "
    "execution and the EUR ones are gone; EUR-only batches (one run of the file node) have all files owned by the "
    "parent. Nothing above debug is logged at the cause. The month-end batch with foreign suppliers is the "
    "trigger, not the defect. "
    "Grading requirements. Localization (D1): accept n8n as the origin, named as the sub-workflow handoff in n8n, "
    "the 'Render remittance advices' sub-workflow / its Execute Workflow Trigger or 'Advice document' node as "
    "executed by n8n, n8n's binary data handling, or n8n-worker / n8n in general; payables-portal, "
    "payments-gateway, PostgreSQL and the ERP's data are not the origin. Required core mechanism (needed to pass; "
    "answer D2-Q1 Yes only if present): (a) the advice files the failing batches need were created by the "
    "sub-workflow and deleted together with the (unsaved, pruned) child execution, because n8n did not copy them "
    "to the parent; and (b) this happens to the files from the earlier run of the child's last node: in batches "
    "with both EUR and foreign-currency lines 'Advice document' runs twice and only the last run's files (the "
    "cross-border ones) are copied to the parent, so the EUR files of mixed batches are lost while EUR-only "
    "batches work. Accept observable equivalents of (b), for example 'when the sub-workflow's last node executes "
    "more than once, n8n only duplicates the binary data of its final execution to the parent' or 'the parent "
    "receives all items, but only the second branch's binaries are re-homed to the parent; the first branch's "
    "still reference the child execution'. Deeper detail that earns credit but is not required (D2-Q2: answer Yes "
    "if at least two are given): duplicateBinaryDataToParent / the trigger 1.2 vs 1.3 output policy, the soft "
    "delete of the unsaved child and the 15-minute hard-delete cycle, failures only when the 10-minute hold spans "
    "a pruning tick (hence intermittent), the binary_data ownership evidence, the affected batch ids. "
    "WRONG mechanisms the evidence invites; a diagnosis that states one of them as the cause must get No for "
    "D2-Q1, D2-Q2, D2-Q3 and D3-Q3, even if it names the failing batches correctly: (1) pruning or the "
    "executions' max age deleting binary data of running or waiting executions, or 'binary data does not survive "
    "a Wait in queue mode', without (b) (the parent's own files survive the hold); (2) 'the child saves no "
    "executions, so its files are deleted' as the whole explanation, without (b) (it does not explain why the "
    "cross-border files and EUR-only batches are fine); (3) the cross-border branch, the foreign currency or the "
    "ERP's data being malformed or producing bad files; (4) PostgreSQL, the binary_data table or the database "
    "binary mode being full, slow or corrupt, or a storage/volume problem; (5) a worker or task runner losing "
    "files in memory, a race between the two workers, or Redis; (6) payables-portal or payments-gateway "
    "rejecting the batch."
)


class N8nPaymentBatchRemittanceFilesLost(Problem):
    LEAD_IN_S = 420
    #: Minutes between n8n-main's pruning hard-delete runs (EXECUTIONS_DATA_HARD_DELETE_INTERVAL default).
    PRUNE_INTERVAL_S = 900
    #: Where the pruning tick should fall inside the batch's 10-minute hold.
    TICK_INTO_HOLD_S = 300
    CROSS_BORDER_SHARE = 0.35
    ATTEMPTS = 2

    def __init__(self):
        super().__init__(app=N8nPaymentBatch())
        self.kubectl = self.app.kubectl
        self.faulty_service = "n8n-worker"
        self.propagation_duration_s = 1200
        self.injected_at = None
        self.trigger: dict = {}
        self.affected: dict[str, list[str]] = {}
        self.root_cause = self.build_structured_root_cause(
            component="deployment/n8n-worker", namespace=self.namespace, description=ROOT_CAUSE
        )
        self.diagnosis_oracle = MechanismGatedJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = N8nPaymentBatchOracle(problem=self)

    @property
    def traffic(self):
        if self.app.traffic is None:
            raise RuntimeError("The payment batch traffic is not running; start the workload first")
        return self.app.traffic

    # ------------------------------------------------------------------ timing
    def _main_started(self) -> float:
        pod = json.loads(self.app.command("get", "pods", "-l", "app.kubernetes.io/component=main", "-o", "json"))[
            "items"
        ]
        if not pod:
            pod = [
                p
                for p in json.loads(self.app.command("get", "pods", "-o", "json"))["items"]
                if p["metadata"]["name"].startswith("n8n-main")
            ]
        started = pod[0]["status"]["containerStatuses"][0]["state"]["running"]["startedAt"]
        return datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()

    def _next_submission(self) -> float:
        """When to submit so that a pruning tick (main start + boot + k * interval) falls into the hold."""
        boot = self._main_started() + 20
        now = time.time()
        k = int((now - boot) // self.PRUNE_INTERVAL_S) + 1
        while boot + k * self.PRUNE_INTERVAL_S - self.TICK_INTO_HOLD_S < now + 5:
            k += 1
        return boot + k * self.PRUNE_INTERVAL_S - self.TICK_INTO_HOLD_S

    # ------------------------------------------------------------------ benign churn
    def _quiet(self) -> bool:
        rows = self.app.psql(
            "SELECT count(*) FROM execution_entity WHERE (status IN ('new', 'running') AND \"deletedAt\" IS NULL) "
            "OR (status = 'waiting' AND \"waitTill\" < now() + interval '90 seconds')"
        )
        return bool(rows) and rows[0][0] == "0"

    def _routine_portal_rollout(self):
        traffic = self.traffic
        traffic.quiesce()
        try:
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline and not self._quiet():
                time.sleep(5)
            self.app.command("rollout", "restart", "deployment/payables-portal")
            self.app.rollout("payables-portal", timeout=300)
            deadline = time.monotonic() + 120
            while True:  # answers again through its NodePort
                try:
                    traffic.portal_batches()
                    break
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(5)
        finally:
            with traffic.lock:
                traffic.background = True

    # ------------------------------------------------------------------ injection
    def _failed(self, batch_id) -> bool:
        problems = self.traffic.check([batch_id])
        return bool(problems) and any("not settled" in i for i in problems.get(batch_id, []))

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: the month-end payment batch pays foreign suppliers together with EUR ones ==")
        traffic = self.traffic
        started = time.time()
        churned = False
        while time.time() - started < self.LEAD_IN_S or not churned:
            time.sleep(15)
            if not churned and time.time() - started > 120:
                self._routine_portal_rollout()
                churned = True
        healthy = [b for b, v in traffic.batches.items() if time.time() - v["submitted"] > 690]
        problems = traffic.check(healthy) if healthy else {}
        if problems:
            raise RuntimeError(f"Batches failed before injection: {problems}")
        for attempt in range(self.ATTEMPTS):
            at = self._next_submission()
            print(f"[inject] month-end batch in {at - time.time():.0f}s (pruning tick expected inside its hold)")
            time.sleep(max(0.0, at - time.time()))
            batch = traffic.month_end_batch()
            if attempt == 0:
                self.injected_at = time.time()
                with traffic.lock:
                    traffic.cross_border_share = self.CROSS_BORDER_SHARE
            print(f"[inject] month-end batch {batch['batch_id']} submitted ({batch['submit_status']})")
            deadline = time.time() + 600 + 300
            while time.time() < deadline:
                time.sleep(20)
                if time.time() - batch["submitted"] > 700 and self._failed(batch["batch_id"]):
                    break
            if time.time() - batch["submitted"] > 700 and self._failed(batch["batch_id"]):
                self.trigger = {"batch": batch["batch_id"], "submitted": batch["submitted"], "attempt": attempt}
                break
            print(f"[inject] month-end batch {batch['batch_id']} settled; next pruning tick")
        else:
            raise RuntimeError("No batch failed after the month-end run")
        self.affected = traffic.check()
        logger.info("n8n injection: trigger=%s affected=%s", json.dumps(self.trigger), json.dumps(self.affected))
        self.mitigation_oracle.capture_handover()
        print(f"[inject] unsettled batches (failed or still held): {sorted(self.affected)}")

    # ------------------------------------------------------------------ reference mitigation and recovery
    def fix_child(self, setting: str = "trigger"):
        app = self.app
        child = app.workflow(name=app.CHILD_WORKFLOW)
        nodes = copy.deepcopy(child["nodes"])
        if setting == "trigger":
            trigger = next(n for n in nodes if n["type"] == "n8n-nodes-base.executeWorkflowTrigger")
            trigger["typeVersion"] = 1.3
            return app.publish_workflow(nodes, child["connections"], name=app.CHILD_WORKFLOW)
        rest = app.rest()
        settings = {**(child.get("settings") or {}), "saveDataSuccessExecution": "all"}
        updated = rest.call(
            "PATCH", f"/rest/workflows/{child['id']}", {"settings": settings, "versionId": child["versionId"]}
        )
        rest.call("POST", f"/rest/workflows/{child['id']}/activate", {"versionId": updated["versionId"]})
        return updated

    def resubmit_unsettled(self):
        traffic = self.traffic
        problems = traffic.check()
        return [traffic.resubmit(batch_id) for batch_id in sorted(problems)]

    def reference_mitigation(self):
        """Upgrade the child's trigger to 1.3 (all files go to the parent), then resubmit the unsettled batches."""
        self.fix_child("trigger")
        time.sleep(5)
        return {"fixed": "Execute Workflow Trigger 1.3", "resubmitted": self.resubmit_unsettled()}

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: child fixed and unsettled batches resubmitted ==")
        if self.app.traffic is None:
            return
        try:
            self.reference_mitigation()
        except Exception as error:  # the namespace may already be gone
            print(f"[recover] reference mitigation failed: {error}")
