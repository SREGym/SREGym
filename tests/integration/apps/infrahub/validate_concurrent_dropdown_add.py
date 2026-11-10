"""Lifecycle check for infrahub_concurrent_dropdown_add_lost_choice, without an agent.

deploy -> healthy grade passes -> inject (the natural event) -> symptom observed
-> mitigation oracle fails -> operator mitigation by hand (kubectl / API, not
recover_fault) -> oracle passes -> recover_fault() -> oracle still passes.

Run inside a SREGym cluster (for example the DinD image), separately from any
benchmark attempt:

    python tests/integration/apps/infrahub/validate_concurrent_dropdown_add.py \
        --problem infrahub_concurrent_dropdown_add_lost_choice --output results/infrahub_concurrent_dropdown_add_lost_choice.json
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from sregym.conductor.problems.apps.infrahub.concurrent_dropdown_add import InfrahubConcurrentDropdownAddLostChoice

PROBLEMS = {
    "infrahub_concurrent_dropdown_add_lost_choice": InfrahubConcurrentDropdownAddLostChoice,
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


def dropdown_symptom(problem):
    """inventory-sync's dc4 registrations are rejected; every worker serves site without dc4."""
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        errors = [e for e in inventory_errors() if "dc4 must be one of" in e.get("error", "")]
        if errors:
            views = problem.app.run_client("choice_views", samples=16)
            return {"inventory_sync_error": errors[-1]["error"], "errors_3m": len(errors), "views": views}
        time.sleep(10)
    raise AssertionError("inventory-sync never reported the rejected dc4 site")


def dropdown_restart_server(problem):
    """Blind: restart the API server (the stored schema still lacks dc4)."""
    kubectl("rollout", "restart", "deployment/infrahub-server")
    kubectl("rollout", "status", "deployment/infrahub-server", "--timeout=600s", timeout=630)
    problem.app.wait_workers_registered()
    return {"restarted": "infrahub-server"}


def dropdown_turn_off(problem):
    """Wrong: stop inventory-sync from using dc4 (the release is turned off)."""
    problem.app.set_inventory_values("site", ["dc1", "dc2", "dc3"])
    return {"inventory_sync_site_values": ["dc1", "dc2", "dc3"]}


def dropdown_turn_on(problem):
    problem.app.set_inventory_values("site", ["dc1", "dc2", "dc3", "dc4"])
    return {"inventory_sync_site_values": ["dc1", "dc2", "dc3", "dc4"]}


def dropdown_readd(problem):
    """Reference: add dc4 again with SchemaDropdownAdd, as an operator would from a toolbox shell."""
    out = operator_python(
        OPERATOR_API
        + """
print(json.dumps(gql('mutation { SchemaDropdownAdd(data: {kind: "NetworkDevice", attribute: "site", '
                    'dropdown: "dc4", label: "DC4 Frankfurt"}) { ok object { value } } }')))
"""
    )
    return {"dropdown_add": out.strip()}


def dropdown_schema_load(problem):
    """Alternative: load NetworkDevice with the complete site and status choice lists (REST schema load)."""
    out = operator_python(
        OPERATOR_API
        + """
def dd(name, choices):
    return {"name": name, "kind": "Dropdown", "optional": True, "choices": [{"name": c} for c in choices]}
node = {"name": "Device", "namespace": "Network", "label": "Network Device", "include_in_menu": True,
        "human_friendly_id": ["name__value"],
        "attributes": [{"name": "name", "kind": "Text", "unique": True},
                       {"name": "role", "kind": "Text", "optional": True},
                       dd("site", ["dc1", "dc2", "dc3", "dc4"]),
                       dd("status", ["active", "planned", "decommissioned", "rma"])]}
print(json.dumps(call("/api/schema/load?branch=main", {"schemas": [{"version": "1.0", "nodes": [node]}]})))
"""
    )
    return {"schema_load": out.strip()[:600]}


def dropdown_evidence(problem):
    """What an SRE sees in the API server log around the two changes."""
    logs = kubectl("logs", "deploy/infrahub-server", timeout=120).splitlines()
    return {"server_schema_lines": [line[:400] for line in logs if "chema" in line][-20:]}


EVIDENCE = {
    "infrahub_concurrent_dropdown_add_lost_choice": dropdown_evidence,
}

SYMPTOM = {
    "infrahub_concurrent_dropdown_add_lost_choice": dropdown_symptom,
}
#: Mitigation scenarios: steps run in order after the symptom, each graded; the last step's
#: grade must pass and every earlier step marked False must fail.
SCENARIOS = {
    "infrahub_concurrent_dropdown_add_lost_choice": {
        "reference": [
            ("blind_restart_server", dropdown_restart_server, False),
            ("turn_release_off", dropdown_turn_off, False),
            ("turn_release_on", dropdown_turn_on, False),
            ("readd_dc4", dropdown_readd, True),
        ],
        "schema_load": [("full_schema_load", dropdown_schema_load, True)],
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
