"""Calibrate the mechanism-gated diagnosis judge of n8n_payment_batch_remittance_files_lost.

One correct, one correct-but-terse, one correct that frames the cause as the
child's save setting but states the copy defect it observed, and three
plausible-but-wrong diagnoses. The wrong ones name the same failed batches and
the same symptom; only the mechanism differs. Each diagnosis is judged
``--repeat`` times (default twice). All correct ones must pass and all wrong
ones must fail.

    uv run --frozen python tests/integration/apps/n8n/judge_payment_batch_remittance_files_lost.py \\
        --output results/n8n-payment-batch-judge.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from sregym.conductor.oracles.apps.n8n.diagnosis import MechanismGatedJudgeOracle  # noqa: E402
from sregym.conductor.problems.apps.n8n.payment_batch_remittance_files_lost import ROOT_CAUSE  # noqa: E402
from sregym.conductor.problems.base import Problem  # noqa: E402

SYMPTOM = (
    "Symptom: payment batches PB-4417, PB-4421 and PB-4424 are still 'scheduled' in payables-portal (WARNING "
    "'payment batch PB-4417 missed its bank cut-off ...: 0 of 5 remittance advices sent, batch not settled'); none "
    "of their lines has a payout at payments-gateway. Their 'Supplier payment batch' executions ended 'error' "
    "after the 10-minute 'Release hold' at 'Read advice': 'Could not find any entity of type \"BinaryDataFile\" "
    "matching fileId ...'. Each of these batches has EUR and foreign-currency lines (GBP, SEK, CHF); EUR-only "
    "batches settle, and mixed batch PB-4419 settled too."
)

DIAGNOSES = {
    "correct": (
        "Root cause: an n8n defect in how a sub-workflow's binary data is handed to its parent. "
        + SYMPTOM
        + " 'Render remittance advices' (Execute Workflow Trigger v1.2, save successful executions: none) sends EUR "
        "lines through 'SEPA advice' and the others through 'Cross-border advice'; both feed 'Advice document', so "
        "in a mixed batch that node runs twice. The parent receives all items of both runs, but n8n copies only "
        "the last run's files (the cross-border ones) to the parent execution: in binary_data the surviving files "
        "of PB-4417 belong to the parent execution, while the EUR items still reference files of the child "
        "execution. The unsaved child is soft-deleted when it ends and n8n-main's pruning hard-delete (every 15 "
        "minutes) removes it with its files; when that happens during the 10-minute hold, 'Read advice' fails on "
        "the first EUR line, so nothing of the batch is paid (PB-4419's hold ended before the next pruning run). "
        "Mitigation: changed the child so all files reach the parent (trigger 1.3) and resubmitted the three "
        "batches; each line is now paid once and advised."
    ),
    "terse": (
        "n8n sub-workflow bug: when 'Advice document' in 'Render remittance advices' runs twice (EUR and "
        "cross-border branch in the same batch), n8n only copies the binaries of its last run to the parent; the "
        "EUR advice files stay with the unsaved child execution and are deleted with it by pruning, so 'Read "
        "advice' fails after the hold (PB-4417, PB-4421, PB-4424 unpaid). Fixed the child and resubmitted the "
        "batches."
    ),
    "observed_copy_gap": (
        "Root cause is in n8n workflow configuration, not infrastructure. The sub-workflow 'Render remittance advices' "
        "(Dz1mNyao5BgqeOnB) had the workflow setting saveDataSuccessExecution='none'. It returns each remittance advice"
        " as binary data (Convert to File), which n8n stores in the database binary_data table under the SUB-"
        "execution's id. With save='none', n8n soft-deletes the finished sub-execution right away (deletedAt set past "
        "the 1h hard-delete buffer), and the hard-delete pruner (every 15 min) then removes the execution plus its "
        "binary data. The parent 'Supplier payment batch' still references those files (only some advice files get "
        "copied under the parent execution id) and holds them through the 10-minute 'Release hold' Wait node. When a "
        "prune cycle falls inside the hold, the 'Read advice' (Extract from File) node fails with NodeOperationError "
        "'Could not find any entity of type BinaryDataFile'. The whole batch errors: no payouts scheduled at payments-"
        "gateway, no remittance advices sent, batch never settled. payables-portal then flags it as having missed its "
        "bank cut-off (PB-4801, PB-4811, then PB-4818/4819/4820). All pods, Redis, Postgres, payables-portal and "
        "payments-gateway are healthy; the payables-portal restart was unrelated. In binary_data, for mixed batches "
        "only the cross-border advice files were copied to the parent execution; the EUR ones still belong to the child"
        " execution, while EUR-only batches have all their files under the parent. So n8n copies only the files of the "
        "last run of 'Advice document' (which runs once per branch) to the parent, and the EUR files are deleted with "
        "the child."
    ),
    "wrong_pruning": (
        "Root cause: n8n's execution pruning deletes binary data of executions that are still waiting. "
        + SYMPTOM
        + " The parent parks in 'Release hold' for 10 minutes; n8n-main's pruning hard-delete cycle runs every 15 "
        "minutes and removes binary data it considers orphaned, including the advice files the waiting parent "
        "still needs, so batches whose hold spans a pruning run fail and others survive. Mitigation: disabled "
        "pruning (EXECUTIONS_DATA_PRUNE=false) and resubmitted the batches."
    ),
    "wrong_unsaved_child": (
        "Root cause: the sub-workflow 'Render remittance advices' saves no successful executions. "
        + SYMPTOM
        + " Its executions are deleted after they finish and n8n's pruning removes their binary data, so the "
        "advice files the parent reads after its 10-minute hold no longer exist. Mitigation: set the child to "
        "save successful executions and resubmitted the batches."
    ),
    "wrong_cross_border": (
        "Root cause: the cross-border branch of 'Render remittance advices' breaks the advice documents of mixed "
        "batches. "
        + SYMPTOM
        + " When a batch contains foreign-currency lines, 'Cross-border advice' produces items whose binary "
        "property overwrites the shared 'data' property of the EUR items, so the EUR documents written by "
        "'Advice document' are invalid and are not stored in the database binary-data table. Mitigation: gave the "
        "cross-border documents their own binary property and resubmitted the batches."
    ),
}
EXPECTED = {"correct": True, "terse": True, "observed_copy_gap": True}


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
                f"no={results[key]['no_answers']} gated={result.get('gated_by')} {'OK' if good else 'MISMATCH'}",
                flush=True,
            )
    Path(args.output).write_text(json.dumps({"diagnoses": DIAGNOSES, "results": results, "all_ok": ok}, indent=1))
    print("ALL OK" if ok else "SOME VERDICTS WRONG")


if __name__ == "__main__":
    main()
