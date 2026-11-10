"""Lifecycle check for infrahub_worker_restart_reopens_release, without an agent.

deploy -> healthy grade passes -> inject (the natural event) -> symptom observed
-> mitigation oracle fails -> operator mitigation by hand (kubectl / API, not
recover_fault) -> oracle passes -> recover_fault() -> oracle still passes.

Run inside a SREGym cluster (for example the DinD image), separately from any
benchmark attempt:

    python tests/integration/apps/infrahub/validate_worker_restart_reopens_release.py \
        --problem infrahub_worker_restart_reopens_release --output results/infrahub_worker_restart_reopens_release.json
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from sregym.conductor.problems.apps.infrahub.worker_restart_reopens_release import InfrahubWorkerRestartReopensRelease

PROBLEMS = {
    "infrahub_worker_restart_reopens_release": InfrahubWorkerRestartReopensRelease,
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


def operator_python(source, timeout=900):
    """What an operator would run from the toolbox pod: a short script against the API."""
    return kubectl("exec", "-i", "application-client", "--", "python", "-", input_text=source, timeout=timeout)


OPERATOR_API = """
import json, urllib.request
TOKEN = open("/credentials/admin-token").read().strip()
def call(path, body):
    req = urllib.request.Request("http://infrahub-server:8000" + path, data=json.dumps(body).encode(),
        headers={"X-INFRAHUB-KEY": TOKEN, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]
def gql(query, variables=None):
    return call("/graphql/main", {"query": query, "variables": variables or {}})
"""


def inventory_errors(since="3m"):
    logs = kubectl("logs", "deploy/inventory-sync", f"--since={since}")
    return [json.loads(line) for line in logs.splitlines() if line.startswith("{") and '"ok": false' in line]


def release_symptom(problem):
    """inventory-sync's release check keeps re-merging the reopened release, and the merge fails."""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        failures = [e for e in inventory_errors("5m") if e.get("op") == "release"]
        if failures:
            return {"inventory_sync_error": failures[-1], "failures_5m": len(failures), "state": problem._state()}
        time.sleep(15)
    raise AssertionError("inventory-sync never reported a failed release merge")


def release_evidence(problem):
    lines = []
    for pod in kubectl("get", "pods", "-l", "app=task-worker", "-o", "name").split():
        for line in kubectl("logs", pod, timeout=120).splitlines():
            if "New schema detected" in line or "MergeConstraintsViolatedError" in line[:300]:
                lines.append(f"{pod}: {line[:300]}")
    rows = problem.app.cypher(
        "MATCH (b:Branch {name: 'lifecycle-eol'}) RETURN b.status, b.merge_started_at, b.updated_at, b.branched_from"
    )
    return {"task_worker_lines": lines[-10:], "branch_vertex": rows}


def release_restart_workers(problem):
    """Blind: restart the task-workers and the API server."""
    for name in ("task-worker", "infrahub-server"):
        kubectl("rollout", "restart", f"deployment/{name}")
    for name in ("task-worker", "infrahub-server"):
        kubectl("rollout", "status", f"deployment/{name}", "--timeout=900s", timeout=930)
    problem.app.wait_workers_registered()
    return {"restarted": ["task-worker", "infrahub-server"], "state": problem._state()}


def release_remove_from_plan(problem):
    """Wrong: silence the symptom by taking the release off inventory-sync's plan."""
    problem.app.set_inventory_releases(["security-zones"])
    time.sleep(90)
    return {"plan": ["security-zones"]}


def release_restore_plan(problem):
    problem.app.set_inventory_releases(["security-zones", "lifecycle-eol"])
    time.sleep(90)
    return {"plan": ["security-zones", "lifecycle-eol"]}


def release_mark_merged(problem):
    """Reference: the release is on main, so set the branch's status back to MERGED (cypher-shell)."""
    out = kubectl(
        "exec",
        "deploy/database",
        "--",
        "sh",
        "-c",
        'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}" "$0"',
        "MATCH (b:Branch {name: 'lifecycle-eol'}) WHERE b.status = 'OPEN' SET b.status = 'MERGED' RETURN b.status",
    )
    return {"cypher": out.strip()}


def release_delete_branch(problem):
    """Alternative: delete the reopened branch, whose changes are already on main."""
    out = operator_python(
        OPERATOR_API
        + """
print(json.dumps(gql('mutation { BranchDelete(data: {name: "lifecycle-eol"}) { ok } }')))
"""
    )
    time.sleep(30)
    return {"branch_delete": out.strip()}


EVIDENCE = {
    "infrahub_worker_restart_reopens_release": release_evidence,
}

SYMPTOM = {
    "infrahub_worker_restart_reopens_release": release_symptom,
}
#: Mitigation scenarios: steps run in order after the symptom, each graded; the last step's
#: grade must pass and every earlier step marked False must fail.
SCENARIOS = {
    "infrahub_worker_restart_reopens_release": {
        "reference": [
            ("blind_restart_workers_and_server", release_restart_workers, False),
            ("remove_release_from_plan", release_remove_from_plan, False),
            ("restore_release_plan", release_restore_plan, False),
            ("mark_branch_merged", release_mark_merged, True),
        ],
        "delete": [("delete_reopened_branch", release_delete_branch, True)],
    },
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
