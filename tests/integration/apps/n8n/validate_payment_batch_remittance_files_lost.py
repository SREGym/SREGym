"""Lifecycle check for n8n_payment_batch_remittance_files_lost, without an agent.

Fault run (default):
    deploy (conductor path, with the observability stack) -> inject -> age the
    incident (propagation_duration_s) -> collect the symptom the way an SRE would
    see it (pod logs, executions, the business systems) -> oracle FAILS ->
    blind fix (retry every failed execution) -> oracle FAILS -> reference (or
    --alternative) mitigation -> oracle PASSES -> recover_fault -> cleanup.

Healthy run (--healthy): deploy -> ordinary traffic only, as long as a fault run
lasts until handover -> no symptom -> oracle on the unfixed deployment (the
defect is latent in upstream n8n, so the recurrence probe fails; the damage
check must pass) -> reference workflow change -> oracle PASSES -> cleanup.

Run inside the SREGym DinD container from the repository root:

    python tests/integration/apps/n8n/validate_payment_batch_remittance_files_lost.py --output results/run1.json
    python tests/integration/apps/n8n/validate_payment_batch_remittance_files_lost.py --alternative --skip-blind --output results/run2.json
    python tests/integration/apps/n8n/validate_payment_batch_remittance_files_lost.py --healthy --output results/healthy.json

``--fast`` shortens the lead-in and the ageing for development runs only.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from logger import init_logger  # noqa: E402
from sregym.conductor.conductor import Conductor, ConductorConfig  # noqa: E402


def kubectl(*args, timeout=300):
    return subprocess.run(["kubectl", "-n", "n8n", *args], capture_output=True, text=True, timeout=timeout).stdout


def logs(deploy, container=None, previous=False):
    args = ["logs", f"deploy/{deploy}", "--timestamps=false"]
    if container:
        args += ["-c", container]
    if previous:
        args.append("--previous")
    return kubectl(*args, timeout=120)


def common_symptom(app):
    main = logs("n8n-main")
    workers = logs("n8n-worker", "n8n-worker")
    gateway = logs("payments-gateway")
    portal = logs("payables-portal")
    executions = app.psql(
        'SELECT w.name, e.status, count(*) FROM execution_entity e JOIN workflow_entity w ON w.id = e."workflowId" '
        "GROUP BY w.name, e.status ORDER BY 1, 2"
    )
    return {
        "executions_by_status": executions,
        "gateway_warnings": [line for line in gateway.splitlines() if " WARNING " in line][-30:],
        "gateway_warning_count": sum(1 for line in gateway.splitlines() if " WARNING " in line),
        "portal_warnings": [line for line in portal.splitlines() if " WARNING " in line][-30:],
        "main_warn_or_error": [line for line in main.splitlines() if re.search(r"\b(warn|error)\b", line, re.I)][-20:],
        "main_resuming_lines": sum(1 for line in main.splitlines() if "Resuming execution" in line),
        "worker_warn_or_error": [line for line in workers.splitlines() if re.search(r"\b(warn|error)\b", line, re.I)][
            -10:
        ],
        "pods": kubectl("get", "pods", "-o", "wide"),
    }


# ---------------------------------------------------------------- problem hooks
class RemittanceFiles:
    """n8n_payment_batch_remittance_files_lost"""

    @staticmethod
    def fast(problem):
        problem.LEAD_IN_S = 240
        problem.propagation_duration_s = 120

    @staticmethod
    def symptom(problem, app):
        traffic = problem.traffic
        rest = app.rest()
        parent = app.find_workflow_id(app.WORKFLOW_NAME, rest)
        failed = app.psql(
            f'SELECT id, "startedAt", "stoppedAt" FROM execution_entity WHERE "workflowId" = \'{parent}\' '
            "AND status = 'error' ORDER BY id"
        )
        errors = {}
        for eid, *_ in failed[:3]:
            data = json.loads(rest.call("GET", f"/rest/executions/{eid}")["data"])
            result = data[int(data[0]["resultData"])]
            error = data[int(result["error"])] if isinstance(result.get("error"), str) else None
            errors[eid] = {
                "lastNodeExecuted": data[int(result["lastNodeExecuted"])],
                "message": data[int(error["message"])][:300]
                if error and isinstance(error.get("message"), str)
                else None,
            }
        files = app.psql(
            'SELECT b."fileName", b."sourceId", w.name FROM binary_data b LEFT JOIN execution_entity e '
            'ON e.id::text = b."sourceId" LEFT JOIN workflow_entity w ON w.id = e."workflowId" ORDER BY b."sourceId" DESC LIMIT 40'
        )
        return {
            "failed_batches": traffic.check(),
            "failed_parent_executions": failed,
            "parent_errors": errors,
            "binary_data_owners_recent": files,
            "portal_batches_scheduled": [b for b in traffic.portal_batches().values() if b["status"] != "settled"],
            "trigger": problem.trigger,
            "traffic": traffic.summary(),
        }

    @staticmethod
    def blind(problem, app):
        """Retry every failed execution through the REST API, as the editor's Retry button does."""
        rest = app.rest()
        parent = app.find_workflow_id(app.WORKFLOW_NAME, rest)
        retried = []
        for (eid,) in app.psql(
            f"SELECT id FROM execution_entity WHERE \"workflowId\" = '{parent}' AND status = 'error' ORDER BY id"
        ):
            try:
                rest.call("POST", f"/rest/executions/{eid}/retry", {"loadWorkflow": True})
                retried.append((eid, "ok"))
            except Exception as error:  # noqa: BLE001
                retried.append((eid, str(error)[:200]))
        time.sleep(60)
        return {"retried": retried}

    @staticmethod
    def alternative(problem, app):
        """Documented alternative: keep the child's successful executions (and its files), then resubmit."""
        problem.fix_child("save")
        time.sleep(5)
        return {
            "fixed": "Render remittance advices saves successful executions",
            "resubmitted": problem.resubmit_unsettled(),
        }

    @staticmethod
    def healthy_fix(problem, app):
        problem.fix_child("trigger")
        return problem.resubmit_unsettled()


PROBLEM = "n8n_payment_batch_remittance_files_lost"
hooks = RemittanceFiles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--healthy", action="store_true")
    parser.add_argument("--skip-blind", action="store_true")
    parser.add_argument("--alternative", action="store_true", help="use the documented alternative mitigation")
    parser.add_argument("--fast", action="store_true", help="development only: short lead-in and ageing")
    args = parser.parse_args()

    record = {"problem": PROBLEM, "healthy": args.healthy, "alternative": args.alternative, "stages": {}}
    started = time.time()

    def stage(name, **data):
        data["t"] = round(time.time() - started, 1)
        record["stages"][name] = data
        print(f"\n===== {name}: {json.dumps(data, default=str)[:6000]}", flush=True)
        Path(args.output).write_text(json.dumps(record, indent=1, default=str))

    init_logger()
    conductor = Conductor(config=ConductorConfig(deploy_loki=False))
    conductor.problem_id = PROBLEM
    problem = conductor.problems.get_problem_instance(PROBLEM)
    conductor.problem, conductor.app = problem, problem.app
    if args.fast:
        hooks.fast(problem)
    oracle = problem.mitigation_oracle
    app = problem.app
    try:
        conductor.dependency_check(["kubectl", "helm", "docker"])
        conductor.fix_kubernetes()
        conductor.undeploy_app()
        t = time.time()
        conductor.deploy_app()
        oracle.capture_baseline()
        stage("deploy", seconds=round(time.time() - t), endpoints=app.endpoints(), workflows=app.workflow_ids)
        stage("agent_facing_description", text=app.description)

        if args.healthy:
            wait = problem.LEAD_IN_S + 900 + problem.propagation_duration_s
            time.sleep(wait)
            stage("healthy_symptom_check", waited=wait, **common_symptom(app), **hooks.symptom(problem, app))
            result = oracle.evaluate()
            stage("oracle_healthy_unfixed", result=result)
            stage("healthy_reference_change", result=hooks.healthy_fix(problem, app))
            result = oracle.evaluate()
            stage("oracle_healthy_with_reference_change", result=result)
            record["passed"] = result.get("success") is True
            return

        t = time.time()
        problem.inject_fault()
        injected = time.time()
        stage("inject", seconds=round(injected - t), injected_at=problem.injected_at)
        time.sleep(problem.propagation_duration_s)
        stage(
            "handover_symptom",
            trigger_to_handover_s=round(time.time() - (problem.injected_at or injected)),
            **common_symptom(app),
            **hooks.symptom(problem, app),
        )
        result = oracle.evaluate()
        stage("oracle_after_inject", result=result)
        if result.get("success") is not False:
            raise SystemExit("oracle did not fail after injection")

        if not args.skip_blind:
            stage("blind_fix", **hooks.blind(problem, app))
            result = oracle.evaluate()
            stage("oracle_after_blind_fix", result=result)
            if result.get("success") is not False:
                raise SystemExit("oracle passed a blind fix")

        if args.alternative:
            stage("alternative_mitigation", **hooks.alternative(problem, app))
        else:
            stage("reference_mitigation", result=problem.reference_mitigation())
        result = oracle.evaluate()
        stage("oracle_after_mitigation", result=result)
        if result.get("success") is not True:
            raise SystemExit("oracle did not pass after the mitigation")

        t = time.time()
        problem.recover_fault()
        stage("recover", seconds=round(time.time() - t))
        record["passed"] = True
    finally:
        record["elapsed_s"] = round(time.time() - started)
        try:
            conductor.problem.app.cleanup()
        except Exception as error:  # noqa: BLE001
            record["cleanup_error"] = str(error)
        Path(args.output).write_text(json.dumps(record, indent=1, default=str))
        print(f"\nRESULT passed={record.get('passed', False)} elapsed={record['elapsed_s']}s", flush=True)


if __name__ == "__main__":
    main()
