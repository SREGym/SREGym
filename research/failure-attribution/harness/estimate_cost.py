"""Rough prompt-size and latency-depth model for each attribution method on Who&When.

No LM calls: prompt tokens are approximated as rendered chars / 4. Usage:
    python -I estimate_cost.py <path to Who&When>
"""

import glob
import json
import math
import os
import sys

OVERHEAD = 1500  # instruction text per call, chars
NOTE = 300  # one per-step note fed to the map/reduce reducer, chars
FOLD_STATE = 1500  # running note carried by the fold, chars


def traces(root, subset):
    field = "role" if subset == "Hand-Crafted" else "name"
    for path in glob.glob(os.path.join(root, subset, "*.json")):
        with open(path) as f:
            j = json.load(f)
        head = len(j["question"]) + len(str(j["ground_truth"]))
        steps = [len(e.get(field, "")) + len(e.get("content", "")) + 12 for e in j["history"]]
        yield head, steps, int(j["mistake_step"])


def per_trace(head, steps, true_step):
    n, full = len(steps), sum(steps)
    prefix = [sum(steps[: i + 1]) for i in range(n)]
    rounds = max(1, math.ceil(math.log2(n)))
    calls = {
        "all_at_once": (1, OVERHEAD + head + full, 1),
        # Re-sends the growing history at every step; stops at its first "yes".
        "step_by_step (no early stop)": (n, sum(OVERHEAD + head + p for p in prefix), n),
        "step_by_step (stops at true step)": (
            true_step + 1,
            sum(OVERHEAD + head + p for p in prefix[: true_step + 1]),
            true_step + 1,
        ),
        # Halves the segment each round.
        "binary_search": (rounds, sum(OVERHEAD + head + full / 2**k for k in range(rounds)), rounds),
        # tracepress: one parallel call per step, then one reduce over the notes.
        "tp map->reduce": (n + 1, sum(OVERHEAD + head + s for s in steps) + OVERHEAD + head + n * NOTE, 2),
        # tracepress lm_fold: one sequential call per step with a fixed-size running note.
        "tp lm_fold": (n, sum(OVERHEAD + head + FOLD_STATE + s for s in steps), n),
    }
    return calls


def main(root):
    for subset in ("Hand-Crafted", "Algorithm-Generated"):
        totals, depth = {}, {}
        count = 0
        for head, steps, true_step in traces(root, subset):
            count += 1
            for name, (c, chars, d) in per_trace(head, steps, true_step).items():
                tc, tch = totals.get(name, (0, 0))
                totals[name] = (tc + c, tch + chars)
                depth[name] = max(depth.get(name, 0), d)
        print(f"\n{subset} ({count} traces)")
        print(f"  {'method':36} {'LM calls':>9} {'prompt Mtok':>12} {'max sequential calls':>21}")
        for name, (c, chars) in totals.items():
            print(f"  {name:36} {c:>9} {chars / 4 / 1e6:>12.2f} {depth[name]:>21}")


if __name__ == "__main__":
    main(sys.argv[1])
