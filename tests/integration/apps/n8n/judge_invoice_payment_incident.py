"""Calibrate the mechanism-gated diagnosis judge of n8n_invoice_payment_incident.

One correct, one correct-but-terse, one correct that names PostgreSQL first, and
three plausible-but-wrong diagnoses. The wrong ones name the same executions,
invoices and events; only the mechanism differs. Each diagnosis is judged
``--repeat`` times (default twice). All correct ones must pass and all wrong
ones must fail.

    uv run --frozen python tests/integration/apps/n8n/judge_invoice_payment_incident.py \\
        --output results/n8n-invoice-judge.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from sregym.conductor.oracles.apps.n8n.diagnosis import MechanismGatedJudgeOracle  # noqa: E402
from sregym.conductor.problems.apps.n8n.invoice_payment_incident import ROOT_CAUSE  # noqa: E402
from sregym.conductor.problems.base import Problem  # noqa: E402

SYMPTOM = (
    "Symptom: eight 'Supplier invoice payment' executions (ids 44-51; invoices INV-17887 to INV-17894, seven of "
    "them one AP batch posted at 07:22:12) are 'crashed' in n8n and show no node data after the 'Approved invoice' "
    "trigger. They were marked crashed at 07:29:20, when n8n-main and the workers were restarted (rollout restart; "
    "n8n-main logged 'Marked executions as `crashed`' and 'Completed queue recovery check, recovered dangling "
    "executions' at start). PostgreSQL was OOMKilled at 07:22:14 (a readiness probe timed out just before) and "
    "accepted connections again at 07:22:42. payments-gateway has exactly one scheduled payout "
    "for each of these invoices (PO-000031 to PO-000038, created 07:22:13), and payables-portal shows them paid "
    "with those payout ids. The ERP's posts that failed while PostgreSQL was down were re-sent and paid normally."
)

DIAGNOSES = {
    "correct": (
        "Root cause: n8n's queue-mode worker lost the completion records of runs that had finished (n8n-worker, "
        "execution persistence). " + SYMPTOM + " The batch's executions were in their 3-second 'Bank confirmation' "
        "wait when PostgreSQL was killed. They carried on in the worker's memory: 'Payout status' and 'Mark invoice "
        "paid' ran, so every invoice was paid once. Then the worker's final save of each finished run (the "
        "workflowExecuteAfter hook, updateExistingExecution) failed with ECONNREFUSED; n8n's engine catch runs the hook once "
        "more, which also failed (the compiled workflow-execute.js only console.errors that second failure), and "
        "the worker still logged 'Worker finished execution N', so the Bull job completed. Nothing retries that "
        "write and main does not write the status, so the rows stayed 'running' with no run data. When n8n-main was "
        "restarted, its start-up queue recovery found running rows with no job in the queue and marked them "
        "crashed. They are not interrupted runs: retrying them would pay the invoices again (Retry is allowed "
        "because finished is false). Mitigation: recorded executions 44-51 as success in execution_entity without "
        "re-running them."
    ),
    "terse": (
        "n8n defect: the batch's payment runs actually completed during the PostgreSQL crash (payouts scheduled, "
        "invoices marked paid), but the worker could not save their final result to the database and never "
        "retried, so the executions stayed running and n8n-main's restart recovery later marked them crashed. "
        "Retrying them would pay twice, so I marked them success."
    ),
    "correct_postgres_first": (
        "Root cause chain: PostgreSQL OOM kill (trigger) -> n8n worker cannot persist finished executions. "
        + SYMPTOM
        + " The runs were already past the payout when the database went away and finished their last node, but "
        "the worker's end-of-run update of the execution failed with ECONNREFUSED and was dropped; n8n has no retry for it, so the rows were "
        "left running and were later flagged crashed by queue recovery when n8n-main restarted. Faulty components: "
        "postgres (OOM, memory limit too low), n8n-worker (no retry of the final execution save). Marked the eight "
        "executions success."
    ),
    "wrong_interrupted": (
        "Root cause: the PostgreSQL OOM kill interrupted the 'Supplier invoice payment' executions that were "
        "running at that moment. " + SYMPTOM + " n8n persists execution progress in PostgreSQL; when the database "
        "connection dropped, the workers could not continue these executions, so they stopped after the payout "
        "request and before 'Bank confirmation' completed. The rows stayed 'running' and queue recovery marked "
        "them crashed at n8n-main's restart. The payment confirmation step and the final bookkeeping of these runs "
        "never completed in n8n, so they must be retried to finish the payment process. Raised the PostgreSQL "
        "memory limit and retried the crashed executions."
    ),
    "wrong_main_restart": (
        "Root cause: the restart of n8n-main at 07:29 crashed the in-flight executions. " + SYMPTOM + " In queue "
        "mode n8n-main tracks running executions; the rollout restarted it while these executions were still "
        "running after the database recovery, and on start-up n8n-main marked every execution it no longer tracked "
        "as crashed ('Marked executions as `crashed`'). The PostgreSQL crash itself was handled: the pods "
        "reconnected. The faulty action is the n8n-main restart without draining executions first. Mitigation: "
        "retried the crashed executions and avoided further main restarts."
    ),
    "wrong_lost_jobs": (
        "Root cause: when PostgreSQL went down, the n8n workers' Bull jobs for these executions stalled and were "
        "dropped from the Redis queue, so the executions were orphaned. " + SYMPTOM + " The workers could not "
        "renew their job locks while their database calls hung, Bull treated the jobs as stalled and removed "
        "them, and with no job in Redis queue recovery later marked the executions crashed. The payouts were "
        "scheduled, but n8n lost the jobs before finishing. Mitigation: retried the crashed executions."
    ),
    "wrong_recovery_overwrote": (
        "Root cause: n8n-main's queue recovery at its restart wrongly marked completed executions as crashed. "
        + SYMPTOM
        + " The batch's runs completed normally: each scheduled its payout, confirmed it and marked the invoice "
        "paid, and the workers reconnected to PostgreSQL after its OOM kill. When the on-call engineer restarted "
        "n8n, the new n8n-main ran its start-up queue recovery, which compares executions with the Bull queue; "
        "these executions no longer had jobs in Redis, so recovery overwrote their finished status with 'crashed' "
        "and dropped their run data. The executions are therefore paid; they must not be retried. Mitigation: set "
        "the eight executions back to success."
    ),
}
EXPECTED = {"correct": True, "terse": True, "correct_postgres_first": True}


class _Stub(Problem):
    def __init__(self):
        self.namespace = "n8n"

    def inject_fault(self):
        pass

    def recover_fault(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--only", action="append")
    args = parser.parse_args()
    expected = Problem.build_structured_root_cause(
        component="deployment/n8n-worker", namespace="n8n", description=ROOT_CAUSE
    )
    oracle = MechanismGatedJudgeOracle(problem=_Stub(), expected=expected)
    results, ok = {}, True
    for name, diagnosis in DIAGNOSES.items():
        if args.only and name not in args.only:
            continue
        for attempt in range(args.repeat):
            result = oracle.evaluate(diagnosis)
            want = EXPECTED.get(name, False)
            good = result.get("success") is want
            ok &= good
            key = f"{name}/{attempt + 1}"
            results[key] = {
                "expected_pass": want,
                "success": result.get("success"),
                "accuracy": result.get("accuracy"),
                "required_items": result.get("required_items"),
                "gated_by": result.get("gated_by"),
                "no_answers": [c["id"] for c in result.get("checklist", []) if c["answer"] == "No"],
                "checklist": result.get("checklist"),
            }
            print(
                f"{key}: success={result.get('success')} accuracy={result.get('accuracy')} "
                f"no={results[key]['no_answers']} {'OK' if good else 'MISMATCH'}",
                flush=True,
            )
    Path(args.output).write_text(json.dumps({"diagnoses": DIAGNOSES, "results": results, "all_ok": ok}, indent=1))
    print("ALL OK" if ok else "SOME VERDICTS WRONG")


if __name__ == "__main__":
    main()
