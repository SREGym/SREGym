"""Run SREGym problems through inject -> oracle -> recover -> oracle, with no agent.

A faithful environment should make the mitigation oracle FAIL right after
injection and PASS after recover_fault().  Running the same script against
kind and against the lightweight cluster lets us compare verdicts directly.

usage: python fidelity.py OUT.jsonl problem_id [problem_id ...]
"""

import json
import logging
import sys
import time
import traceback

logging.basicConfig(level=logging.WARNING)

from sregym.conductor.problems.registry import ProblemRegistry  # noqa: E402
from sregym.service.kubectl import KubeCtl  # noqa: E402

PROPAGATE_S = 30


def ok(r):
    return bool(r and r.get("success"))


def run(pid, registry, kubectl):
    rec = {"problem": pid}
    t0 = time.time()
    try:
        problem = registry.get_problem_instance(pid)
        oracle = problem.mitigation_oracle
        if oracle is None:
            rec["skipped"] = "no mitigation oracle"
            return rec
        oracle.capture_baseline()
        t = time.time()
        problem.inject_fault()
        rec["inject_s"] = round(time.time() - t, 1)
        time.sleep(PROPAGATE_S)
        t = time.time()
        r1 = oracle.evaluate()
        rec["oracle_after_inject"] = ok(r1)
        rec["oracle_after_inject_s"] = round(time.time() - t, 1)
        t = time.time()
        problem.recover_fault()
        rec["recover_s"] = round(time.time() - t, 1)
        time.sleep(PROPAGATE_S)
        t = time.time()
        r2 = oracle.evaluate()
        rec["oracle_after_recover"] = ok(r2)
        rec["oracle_after_recover_s"] = round(time.time() - t, 1)
        # Expected pattern for a faithful environment: fail, then pass.
        rec["faithful"] = (not rec["oracle_after_inject"]) and rec["oracle_after_recover"]
        if not rec["faithful"]:
            rec["detail_inject"] = {k: v for k, v in (r1 or {}).items() if k != "success"}
            rec["detail_recover"] = {k: v for k, v in (r2 or {}).items() if k != "success"}
    except Exception as e:  # keep going; record the failure
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["trace"] = traceback.format_exc()[-1500:]
    finally:
        rec["total_s"] = round(time.time() - t0, 1)
        try:
            kubectl.wait_for_ready("hotel-reservation")
        except Exception as e:
            rec["post_ready_error"] = str(e)[:300]
    return rec


def main():
    out = sys.argv[1]
    registry = ProblemRegistry()
    kubectl = KubeCtl()
    for pid in sys.argv[2:]:
        rec = run(pid, registry, kubectl)
        print(json.dumps({k: v for k, v in rec.items() if k != "trace"}), flush=True)
        with open(out, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")


if __name__ == "__main__":
    main()
