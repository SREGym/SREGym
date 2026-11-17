"""Prove the workspace gateway cascade behaves as designed on a live cluster.

1. A no-op attempt fails grading.
2. The tempting partial fix does not pass: one worker thread per unit,
   rolling back the latest release, reverting chat's failover and returning
   the drained unit. The defect ships in every release since 2026.07, so a
   single unit still fails the grader's load test.
3. The reference repair passes grading.

Run on a cluster with OpenEBS, separately from any benchmark attempt:

    python tests/integration/apps/workspace/validate_gateway_cascade.py --tier single
"""

import argparse
import json
import time

from sregym.conductor.problems.apps.workspace.gateway_cascade import WorkspaceGatewayCascadeProblem
from sregym.service.apps.workspace.application import EDGE_RELEASES, REGIONS


def validate(tier, skip_deploy=False):
    problem = WorkspaceGatewayCascadeProblem(tier)
    app, oracle = problem.app, problem.mitigation_oracle
    started = time.time()
    result = {"tier": tier, "passed": False, "stages": {}}

    def record(stage, value):
        result["stages"][stage] = value
        print(json.dumps({"stage": stage, "elapsed_seconds": round(time.time() - started), "value": value}), flush=True)

    if not skip_deploy:
        app.deploy()
    oracle.capture_baseline()
    problem.inject_fault()
    record("injected", problem.observed)

    noop = oracle.evaluate()
    record("noop_grade", noop)
    if noop.get("success"):
        return result

    # The visible recent changes undone, the latest release rolled back,
    # and worker threads cut to the CPU quota.
    app.set_policy(drained="", **{"placement.chat": "alternate"})
    config = app.command("get", "configmap", "edge-gw", "-o", "jsonpath={.data.edge-gw\\.conf}")
    config = config.replace("worker_threads = auto", "worker_threads = 1")
    app.command("patch", "configmap", "edge-gw", "--type=merge", "-p", json.dumps({"data": {"edge-gw.conf": config}}))
    previous = EDGE_RELEASES[-2][1]
    for region in REGIONS:
        app.command("set", "image", f"statefulset/edge-gw-{region}", f"edge-gw={previous}")
    for region in REGIONS:
        app.command("rollout", "status", f"statefulset/edge-gw-{region}", "--timeout=300s", timeout=330)
    time.sleep(300)
    partial = problem.collapse_state()
    record("partial_fix_state", partial)
    partial_grade = oracle.evaluate()
    record("partial_fix_grade", partial_grade)
    if partial_grade.get("success"):
        return result

    problem.reference_repair()
    repaired = oracle.evaluate()
    record("reference_grade", repaired)
    result["passed"] = bool(repaired.get("success"))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", default="single", choices=("single", "replicated"))
    parser.add_argument("--skip-deploy", action="store_true")
    args = parser.parse_args()
    result = validate(args.tier, args.skip_deploy)
    print(json.dumps(result, indent=1, default=str))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
