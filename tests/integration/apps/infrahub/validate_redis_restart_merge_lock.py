"""Lifecycle check for infrahub_redis_restart_wedged_merge_lock, without an agent.

deploy -> healthy grade passes -> inject (the natural event) -> symptom observed
-> mitigation oracle fails -> operator mitigation by hand (kubectl / API, not
recover_fault) -> oracle passes -> recover_fault() -> oracle still passes.

Run inside a SREGym cluster (for example the DinD image), separately from any
benchmark attempt:

    python tests/integration/apps/infrahub/validate_redis_restart_merge_lock.py \
        --problem infrahub_redis_restart_wedged_merge_lock --output results/infrahub_redis_restart_wedged_merge_lock.json
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from sregym.conductor.problems.apps.infrahub.redis_restart_merge_lock import InfrahubRedisRestartWedgedMergeLock

PROBLEMS = {
    "infrahub_redis_restart_wedged_merge_lock": InfrahubRedisRestartWedgedMergeLock,
}


def kubectl(*args, input_text=None, timeout=300):
    return subprocess.run(
        ["kubectl", "-n", "infrahub", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    ).stdout


def merge_symptom(problem):
    """A fresh change branch submitted for merge does not complete; the merge lock names the wedged worker."""
    try:
        result = problem.app.run_client("branch_change", devices=1, timeout=180)
    except Exception as exc:  # branch creation can hang too when it lands on the wedged worker
        result = {"merged": False, "error": str(getattr(exc, "stderr", "") or exc)[-500:]}
    assert not result["merged"], result
    holder = problem.app.redis("get", "lock.merge.all_branches").strip()
    pods = kubectl("get", "pods", "-o", "wide")
    return {"probe_merge": result, "merge_lock": holder, "pods": pods}


def acknowledged_writes(problem):
    """Every device inventory-sync registered (and read back) on main must still exist."""
    logs = kubectl("logs", "deploy/inventory-sync", timeout=120)
    events = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
    names = [e["name"] for e in events if e.get("op") == "register" and e.get("kind") == "NetworkDevice" and e["ok"]]
    missing = problem.app.run_client("missing_devices", names=names, exec_timeout=600)["missing"]
    return {"acknowledged": len(names), "missing": missing}


def merge_manual_mitigation(problem):
    steps = {"acknowledged_writes_before": acknowledged_writes(problem)}
    holder = kubectl("exec", "deploy/cache", "--", "redis-cli", "get", "lock.merge.all_branches").strip()
    identity = holder.partition("::")[2]
    steps["merge_lock"] = holder
    wedged = []
    for pod in kubectl("get", "pods", "-l", "app=task-worker", "-o", "name").split():
        if identity and identity in kubectl("logs", pod, timeout=120):
            wedged.append(pod)
    steps["wedged_pods"] = wedged
    assert wedged, "could not attribute the merge lock to a task-worker pod"
    for pod in wedged:
        kubectl("delete", pod, "--wait=true", timeout=180)
    kubectl("rollout", "status", "deployment/task-worker", "--timeout=600s", timeout=630)
    time.sleep(30)
    active = kubectl("exec", "deploy/cache", "--", "redis-cli", "exists", f"workers:active:git_agent:worker:{identity}")
    if active.strip() == "0":
        # Holder is gone: delete its merge lock now instead of waiting for the 15-minute reaper.
        kubectl("exec", "deploy/cache", "--", "redis-cli", "del", "lock.merge.all_branches")
        steps["deleted_stale_merge_lock"] = True
    branches = problem.app.run_client("branches")
    steps["branches_in_merge_state"] = {b: s for b, s in branches.items() if s in ("MERGING", "MERGE_FAILED")}
    # The healthy task-worker keeps running: its queued merges resume as soon as the lock is gone.
    # Recover only merges flagged MERGE_FAILED.
    if "MERGE_FAILED" in steps["branches_in_merge_state"].values():
        steps["recover_merge"] = kubectl(
            "exec", "deploy/infrahub-server", "--", "infrahub", "recover", "merge", "--yes"
        )
    assert not steps["acknowledged_writes_before"]["missing"], steps["acknowledged_writes_before"]
    return steps


def inventory_errors(since="3m"):
    logs = kubectl("logs", "deploy/inventory-sync", f"--since={since}")
    return [json.loads(line) for line in logs.splitlines() if line.startswith("{") and '"ok": false' in line]


EVIDENCE = {}

SYMPTOM = {
    "infrahub_redis_restart_wedged_merge_lock": merge_symptom,
}
#: Mitigation scenarios: steps run in order after the symptom, each graded; the last step's
#: grade must pass and every earlier step marked False must fail.
SCENARIOS = {
    "infrahub_redis_restart_wedged_merge_lock": {"reference": [("manual", merge_manual_mitigation, True)]},
}


def validate(problem_id, keep=False, scenario="reference", age=0):
    problem = PROBLEMS[problem_id]()
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"problem": problem_id, "passed": False, "timings": {}}
    started = time.time()
    stage = "start"

    def timed(name, fn):
        t = time.time()
        value = fn()
        result["timings"][name] = round(time.time() - t, 1)
        return value

    def grade(name):
        outcome = timed(f"grade_{name}", oracle.evaluate)
        print(json.dumps({"stage": name, "elapsed": round(time.time() - started), "grade": outcome}), flush=True)
        result[f"grade_{name}"] = outcome
        return outcome

    try:
        stage = "deploy"
        timed("deploy", app.deploy)
        timed("start_workload", app.start_workload)
        stage = "healthy"
        timed("baseline", oracle.capture_baseline)
        assert grade("healthy")["success"]
        result["healthy_inventory_errors"] = inventory_errors("5m")[-5:]
        stage = "inject"
        timed("inject", problem.inject_fault)
        result["observed"] = problem.observed
        print(json.dumps({"stage": "injected", "observed": problem.observed}, default=str), flush=True)
        if age:
            time.sleep(age)  # the incident ages as before handover
        stage = "symptom"
        result["symptom"] = timed("symptom", lambda: SYMPTOM[problem_id](problem))
        print(json.dumps({"stage": "symptom", "symptom": result["symptom"]}, default=str), flush=True)
        if problem_id in EVIDENCE:
            result["evidence"] = EVIDENCE[problem_id](problem)
        stage = "oracle_fails"
        assert not grade("faulty")["success"]
        result["steps"] = {}
        for name, fn, expect in SCENARIOS[problem_id][scenario]:
            stage = f"step_{name}"
            result["steps"][name] = timed(name, lambda fn=fn: fn(problem))
            print(json.dumps({"stage": name, "steps": result["steps"][name]}, default=str), flush=True)
            verdict = grade(name)
            assert bool(verdict["success"]) == expect, f"{name}: expected success={expect}"
        stage = "recover_fault"
        timed("recover_fault", problem.recover_fault)
        recovered = grade("recovered")["success"]
        # A scenario that ends in a failing (damaging) step cannot be recovered: lost data stays lost.
        if SCENARIOS[problem_id][scenario][-1][2]:
            assert recovered
        result["passed"] = True
    except Exception as exc:
        result["error"] = f"{stage}: {type(exc).__name__}: {exc}"
        if stderr := getattr(exc, "stderr", None):
            result["stderr"] = str(stderr)[-4000:]
    finally:
        if not keep:
            try:
                app.cleanup()
            except Exception as exc:
                result["cleanup_error"] = str(exc)
    result["elapsed_seconds"] = round(time.time() - started, 1)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--problem", choices=sorted(PROBLEMS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep", action="store_true", help="Leave the namespace for inspection")
    parser.add_argument("--scenario", default="reference", help="Mitigation scenario (see SCENARIOS)")
    parser.add_argument("--age", type=int, default=0, help="Seconds the incident ages before the symptom check")
    args = parser.parse_args()
    outcome = validate(args.problem, keep=args.keep, scenario=args.scenario, age=args.age)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outcome, indent=2, default=str) + "\n")
    print(json.dumps({k: v for k, v in outcome.items() if k != "observed"}, indent=2, default=str))
    raise SystemExit(int(not outcome["passed"]))
