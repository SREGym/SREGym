"""Calibrate the diagnosis judge of pretix_waitinglist_closes_quota.

One full correct diagnosis, one correct-but-terse diagnosis and two
plausible-but-wrong ones (all name the same component and symptom; only the mechanism
differs), each judged ``--repeat`` times. All correct ones must pass, all wrong must fail.

    JUDGE_MODEL_ID=anthropic/claude-opus-5-5 \
    ANTHROPIC_API_KEY=... python tests/integration/apps/pretix/judge_waitinglist_closes_quota.py --output results/pretix-judge.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle  # noqa: E402
from sregym.conductor.problems.apps.pretix import waitinglist_closes_quota as CLOSE  # noqa: E402
from sregym.conductor.problems.base import Problem  # noqa: E402

CLOSE_SYMPTOM = (
    "Symptom: the door station warns 'Room B1 (Practical PostgreSQL performance): 2 of 16 seats free, 5 waiting for a "
    "seat (longest 31 min)'. The workshop's quota is closed and the shop shows it SOLD OUT, but it has 14 paid "
    "registrations for 16 seats; its waiting list has 5 entries and none got a voucher (no 'selected from the "
    "waitinglist' mails). The quota log shows 'pretix.event.quota.closed' at 10:42:31 with no user or token; "
    "close_when_sold_out is on for all workshop quotas. Reopening the quota via the API was undone within the same "
    "second (quota.opened then quota.closed again)."
)

CLOSE_DIAGNOSES = {
    "correct": (
        "Root cause: a pretix defect in the quota availability / close-when-sold-out logic (computed in pretix-web "
        "requests; the pretix-periodic cron's waiting-list assignment then sees the closed quota). "
        + CLOSE_SYMPTOM
        + " At 10:32 two visitors held the last two seats in carts and abandoned them; while the seats were reserved "
        "four visitors joined the waiting list. The carts expired at 10:42:30 and the next shop page view computed "
        "availability with the waiting list: QuotaAvailability._compute_waitinglist subtracts the pending entries "
        "from the 2 free seats and returns AVAILABILITY_ORDERED (0), and compute() always calls _close(), which "
        "permanently closes a close_when_sold_out quota at <= ORDERED (carts alone give RESERVED and would not close "
        "it). So the waiting list itself 'sold out' the quota. assign_automatically counts without the waiting list "
        "but gets 0 for a closed quota, so no vouchers are sent; a plain reopen is re-closed by the next computation "
        "(Quota.rebuild_cache in the API update). Fix: reopened with close_when_sold_out switched off in the same "
        "PATCH, the next cron run sent vouchers to the first two entries, then switched close_when_sold_out back on."
    ),
    "correct_terse": (
        "Root cause: close_when_sold_out closed the Practical PostgreSQL performance quota (14/16 sold) because "
        "pretix counts the waiting-list entries against the free seats: after two abandoned carts expired, the "
        "waiting list alone made the quota look sold out and the next availability check closed it. Closed quotas "
        "report 0 to the waiting-list assignment, so the waiting list is never served, and a plain reopen closes "
        "again immediately. Fix: reopened it with close-when-sold-out off and let the cron send the vouchers."
    ),
    "wrong_cancellations": (
        "Root cause: the close_when_sold_out setting on the Practical PostgreSQL performance quota. "
        + CLOSE_SYMPTOM
        + " The workshop sold out earlier this morning (16 registrations), which closed it as configured; since then "
        "two registrations were cancelled or expired, and close_when_sold_out by design keeps the quota closed and "
        "does not hand freed seats to the waiting list. The re-close after reopening is the same setting reacting to "
        "the full waiting list. Fix: reopened the quota with close_when_sold_out off so the cron sends the vouchers."
    ),
    "wrong_stale_cache": (
        "Root cause: a stale quota availability cache in Redis. " + CLOSE_SYMPTOM + " While the two abandoned carts "
        "held the last seats, pretix cached the quota as 0 available in Redis (quotas:1:availabilitycache, 7-day "
        "expiry). After the carts expired, the shop and the cron's waiting-list assignment kept reading that cached "
        "0, so the waiting list was never offered the seats and close_when_sold_out closed the quota on the cached "
        "sold-out value. Fix: deleted the Redis availability cache keys, reopened the quota with close_when_sold_out "
        "off and let the cron send the vouchers."
    ),
}

CASES = {
    "pretix_waitinglist_closes_quota": (CLOSE.ROOT_CAUSE, "deployment/pretix-web", CLOSE_DIAGNOSES),
}


class _Stub(Problem):
    def __init__(self):
        self.namespace = "pretix"

    def inject_fault(self):
        pass

    def recover_fault(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--problem", action="append")
    args = parser.parse_args()
    out = {}
    for pid, (root_cause, component, diagnoses) in CASES.items():
        if args.problem and pid not in args.problem:
            continue
        expected = Problem.build_structured_root_cause(component=component, namespace="pretix", description=root_cause)
        oracle = LLMAsAJudgeOracle(problem=_Stub(), expected=expected)
        out[pid] = {"diagnoses": diagnoses, "results": {}}
        for name, diagnosis in diagnoses.items():
            runs = []
            for _ in range(args.repeat):
                r = oracle.evaluate(diagnosis)
                runs.append(
                    {
                        "success": r.get("success"),
                        "accuracy": r.get("accuracy"),
                        "dimensions": r.get("dimensions"),
                        "checklist": r.get("checklist"),
                    }
                )
                print(
                    f"{pid} {name}: success={r.get('success')} accuracy={r.get('accuracy')} "
                    f"{ {k: v.get('score') for k, v in (r.get('dimensions') or {}).items()} }",
                    flush=True,
                )
            out[pid]["results"][name] = runs
        Path(args.output).write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
