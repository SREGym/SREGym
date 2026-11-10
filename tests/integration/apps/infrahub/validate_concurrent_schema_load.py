"""Lifecycle check for infrahub_concurrent_schema_load_lost_attribute, without an agent.

deploy -> healthy grade passes -> inject (the natural event) -> symptom observed
-> mitigation oracle fails -> operator mitigation by hand (kubectl / API, not
recover_fault) -> oracle passes -> recover_fault() -> oracle still passes.

Run inside a SREGym cluster (for example the DinD image), separately from any
benchmark attempt:

    python tests/integration/apps/infrahub/validate_concurrent_schema_load.py \
        --problem infrahub_concurrent_schema_load_lost_attribute --output results/infrahub_concurrent_schema_load_lost_attribute.json
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from sregym.conductor.problems.apps.infrahub.concurrent_schema_load import InfrahubConcurrentSchemaLoadLostAttribute

PROBLEMS = {
    "infrahub_concurrent_schema_load_lost_attribute": InfrahubConcurrentSchemaLoadLostAttribute,
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


def schema_symptom(problem):
    """inventory-sync's device registrations are rejected and the lost attribute is not served."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        logs = kubectl("logs", "deploy/inventory-sync", "--since=2m")
        errors = [json.loads(line) for line in logs.splitlines() if line.startswith("{") and '"ok": false' in line]
        if any("is not defined" in e.get("error", "") for e in errors):
            views = problem.app.run_client("views", kinds=["NetworkDevice", "NetworkModule"])
            return {"inventory_sync_error": errors[-1]["error"], "views": views}
        time.sleep(10)
    raise AssertionError("inventory-sync never reported the undefined-field error")


def schema_manual_mitigation(problem):
    """Load the complete intended schema once, then make sure every API worker serves it."""
    released = problem.mitigation_oracle.release
    load = problem.app.run_client(
        "load_schema", extra=released["NetworkDevice"], module_extra=released["NetworkModule"], exec_timeout=900
    )
    steps = {"full_schema_load": load}
    expected = {kind: ["name", *fields] for kind, fields in problem.mitigation_oracle.fields.items()}
    steps["converged"] = problem.app.run_client("wait_views", expected=expected, timeout=90, exec_timeout=600)
    if not steps["converged"]["converged"]:
        kubectl("rollout", "restart", "deployment/infrahub-server")
        kubectl("rollout", "status", "deployment/infrahub-server", "--timeout=600s", timeout=630)
        steps["restarted_server"] = True
    return steps


def inventory_errors(since="3m"):
    logs = kubectl("logs", "deploy/inventory-sync", f"--since={since}")
    return [json.loads(line) for line in logs.splitlines() if line.startswith("{") and '"ok": false' in line]


EVIDENCE = {}

SYMPTOM = {
    "infrahub_concurrent_schema_load_lost_attribute": schema_symptom,
}
#: Mitigation scenarios: steps run in order after the symptom, each graded; the last step's
#: grade must pass and every earlier step marked False must fail.
SCENARIOS = {
    "infrahub_concurrent_schema_load_lost_attribute": {"reference": [("manual", schema_manual_mitigation, True)]},
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
