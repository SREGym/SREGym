"""Lifecycle check for dependency_track_dependency_reviews_missing, without an agent.

deploy (conductor path) -> healthy oracle passes -> inject (the natural events
plus the driver) -> handover checks (no driver leftovers) -> propagation wait ->
symptom through ordinary observability -> mitigation oracle fails -> blind fixes
(re-analysis, api-server restart, re-running the release pipeline) leave it
failing -> operator mitigation by hand (``--variant``) -> oracle passes ->
recover_fault() -> oracle still passes.

``--healthy`` runs the same timeline with the release imported normally (the
configuration change is applied after the import) and checks that the release's
reviews arrive and the oracle passes.

    python tests/integration/apps/dependency_track/validate_dependency_reviews_missing.py results/out.json [--variant NAME]
    python tests/integration/apps/dependency_track/validate_dependency_reviews_missing.py results/healthy.json --healthy
"""

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from logger import init_logger  # noqa: E402
from sregym.conductor.conductor import Conductor, ConductorConfig  # noqa: E402
from sregym.conductor.problems.apps.dependency_track.dependency_reviews_missing import (  # noqa: E402
    DependencyTrackDependencyReviewsMissing,
)

PROBLEM = "dependency_track_dependency_reviews_missing"
VARIANTS = ("reference", "delete_and_rerun", "two_uploads")

NS = "dependency-track"


def kubectl(*args, input_text=None, timeout=300):
    return subprocess.run(
        ["kubectl", "-n", NS, *args], input=input_text, capture_output=True, text=True, timeout=timeout
    ).stdout


def logs(deployment, since=None, grep=None, tail=None):
    args = ["logs", f"deploy/{deployment}"]
    if since:
        args.append(f"--since={since}")
    lines = kubectl(*args, timeout=180).splitlines()
    if grep:
        lines = [line for line in lines if grep in line]
    return lines[-tail:] if tail else lines


def api_warnings(since="40m"):
    lines = logs("dt-api-server", since=since)
    out = {}
    for line in lines:
        for level in (" WARN ", " ERROR "):
            if level in line:
                key = line.split(level, 1)[1].split("[", 1)[1].split("]", 1)[0] if "[" in line else "?"
                msg = line.split("] ", 1)[1][:90] if "] " in line else line[:90]
                out[f"{level.strip()} {key}: {msg}"] = out.get(f"{level.strip()} {key}: {msg}", 0) + 1
    return out


def driver_leftovers(problem):
    """Anything the conductor's driver could have left behind: extra DB sessions, exec processes, pods."""
    sessions = problem.app.psql(
        "SELECT application_name, state, count(*) FROM pg_stat_activity WHERE datname = 'dtrack' "
        "AND pid <> pg_backend_pid() GROUP BY 1, 2 ORDER BY 1, 2"
    )
    locks = problem.app.psql(
        "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
        "WHERE a.datname = 'dtrack' AND (l.locktype = 'tuple' OR NOT l.granted)"
    )
    pods = kubectl("get", "pods", "-o", "custom-columns=NAME:.metadata.name,PHASE:.status.phase", "--no-headers")
    postgres = [line for line in logs("postgres") if "WARNING" in line or "ERROR" in line or "FATAL" in line]
    return {
        "sessions": sessions.strip().splitlines(),
        "waiting_or_tuple_locks": locks.strip(),
        "pods": pods.split(),
        "postgres_warnings": postgres[-10:],
    }


class Recorder:
    def __init__(self, path, problem_id):
        self.path = path
        self.started = time.time()
        self.record = {"problem": problem_id, "stages": {}}

    def __call__(self, name, **data):
        data["t"] = round(time.time() - self.started, 1)
        self.record["stages"][name] = data
        print(f"\n===== {name}: {json.dumps(data, default=str)[:4000]}", flush=True)
        Path(self.path).write_text(json.dumps(self.record, indent=1, default=str))


def poll(oracle, want, timeout):
    deadline = time.time() + timeout
    while True:
        result = oracle.evaluate()
        if result.get("success") is want or time.time() > deadline:
            return result
        time.sleep(30)


def observe(problem):
    app, incident = problem.app, problem.incident
    uuid = incident["project_uuid"]
    metrics = app.ops("Http(REVIEW).call('GET', '/metrics', raw=True)[2]", timeout=60)
    runs = app.admin_call("workflow_runs", uuid, None, 100, timeout=300)
    items = runs.get("items", []) if isinstance(runs, dict) else []
    pods = kubectl(
        "get",
        "pods",
        "-o",
        "custom-columns=NAME:.metadata.name,START:.status.startTime,RESTARTS:.status.containerStatuses[0].restartCount",
        "--no-headers",
    )
    api_log = logs("dt-api-server")
    name = f"projectName={incident['project']}"
    return {
        "review_audit_lines": logs("dependency-review", grep="without review", tail=3),
        "review_unreviewed_metric": [line for line in metrics.splitlines() if "unreviewed" in line][-4:],
        "review_audit": app.ops("review_audit()", timeout=60),
        "reviews_for_release": len(app.ops(f"reviews({uuid!r})", timeout=60)),
        "reviews_total": len(app.ops("reviews()", timeout=60)),
        "release_findings": app.admin_call("findings", uuid),
        "release_tickets": len(app.ops(f"tickets({uuid!r})", timeout=60)),
        "release_violations": len(app.admin_call("violations", uuid, timeout=300)),
        "ci_release_lines": [line[:300] for line in logs("ci-pipelines", grep=f'"{incident["project"]}"')][-6:],
        "release_runs": [
            {k: r.get(k) for k in ("workflow_name", "status", "created_at", "completed_at")}
            for r in sorted(items, key=lambda r: r["created_at"])
        ],
        "api_server_import_lines": [line[:260] for line in api_log if name in line and "BOM" in line][-4:],
        "api_server_first_line": api_log[:1],
        "api_server_warn_error": api_warnings(since="90m"),
        "pods": pods.splitlines(),
        "events": kubectl(
            "get",
            "events",
            "--sort-by=.lastTimestamp",
            "-o",
            "custom-columns=T:.lastTimestamp,R:.reason,O:.involvedObject.name",
            "--no-headers",
        ).splitlines()[-15:],
        "context_files": kubectl(
            "exec",
            "deploy/seaweedfs",
            "--",
            "sh",
            "-c",
            "echo 'fs.ls /buckets/dtrack/vuln-analysis/context' | weed shell -master=localhost:9333 2>&1 | tail -5",
            timeout=120,
        ).splitlines(),
    }


def blind_fixes(problem):
    app, incident = problem.app, problem.incident
    uuid = incident["project_uuid"]
    app.admin_call("analyze", uuid, as_admin=True)
    time.sleep(180)
    kubectl("rollout", "restart", "deployment/dt-api-server")
    kubectl("rollout", "status", "deployment/dt-api-server", "--timeout=900s", timeout=930)
    app.wait_api_ready(timeout=900)
    repository = next(r for r, n, v in problem.RELEASES if n == incident["project"] and v == incident["version"])
    problem.push(repository, incident["version"])
    problem.wait_ci_idle()
    time.sleep(60)
    return ["re-analysis", "api-server restart", "release pipeline re-run"]


def mitigate(problem, variant):
    app, incident = problem.app, problem.incident
    uuid = incident["project_uuid"]
    if variant == "reference":
        return app.admin_call("reimport_components", uuid, as_admin=True, timeout=900)
    vulnerable = sorted({f["component_uuid"] for f in app.admin_call("findings", uuid)})
    if variant == "delete_and_rerun":
        for component in vulnerable:
            app.ops(f"admin({app.admin_password!r}).ok('DELETE', '/api/v1/component/{component}')", timeout=120)
        repository = next(r for r, n, v in problem.RELEASES if n == incident["project"] and v == incident["version"])
        problem.push(repository, incident["version"])
        problem.wait_ci_idle()
        return {"deleted": len(vulnerable), "rerun": f"{repository}@{incident['version']}"}
    if variant == "two_uploads":
        bom = app.ops(
            f"admin({app.admin_password!r}).call('GET', "
            f"'/api/v1/bom/cyclonedx/project/{uuid}?format=json&variant=inventory', raw=True)[2]",
            timeout=120,
        )
        document = json.loads(bom)
        purls = {f["component"] for f in app.admin_call("findings", uuid)}
        reduced = {**document, "components": [c for c in document["components"] if c.get("purl") not in purls]}
        for body in (reduced, document):
            payload = {"project": uuid, "bom": base64.b64encode(json.dumps(body).encode()).decode()}
            token = app.ops(f"admin({app.admin_password!r}).ok('PUT', '/api/v1/bom', {payload!r})['token']")
            deadline = time.time() + 600
            while time.time() < deadline:
                time.sleep(5)
                state = app.ops(f"admin({app.admin_password!r}).ok('GET', '/api/v1/event/token/{token}')")
                if not state.get("processing", False):
                    break
        return {"removed_then_restored": sorted(purls)}
    raise ValueError(variant)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--healthy", action="store_true", help="the same timeline without the driver")
    parser.add_argument("--variant", default="reference", choices=VARIANTS)
    parser.add_argument("--propagation", type=int, default=None, help="override propagation_duration_s")
    args = parser.parse_args()
    stage = Recorder(args.output, PROBLEM)

    init_logger()
    conductor = Conductor(config=ConductorConfig(deploy_loki=False))
    conductor.problem_id = PROBLEM
    problem = DependencyTrackDependencyReviewsMissing(inject=not args.healthy)
    conductor.problem, conductor.app = problem, problem.app
    oracle = problem.mitigation_oracle
    try:
        t = time.time()
        conductor.dependency_check(["kubectl", "helm", "docker"])
        conductor.fix_kubernetes()
        conductor.undeploy_app()
        conductor.deploy_app()
        oracle.capture_baseline()
        stage("deploy", seconds=round(time.time() - t), profile=problem.app.profile)
        stage("healthy_oracle", result=oracle.evaluate())
        stage("description", text=problem.app.description)

        t = time.time()
        problem.inject_fault()
        stage(
            "inject",
            seconds=round(time.time() - t),
            attempts=problem.attempts,
            incident=problem.incident,
            events=problem.events,
        )
        stage("handover_footprint", **driver_leftovers(problem))
        stage("expected_root_cause", text=problem.root_cause)
        propagation = problem.propagation_duration_s if args.propagation is None else args.propagation
        stage(
            "propagation_wait",
            seconds=propagation,
            trigger_to_handover_s=round(time.time() - problem.incident["rollout_at"] + propagation),
        )
        time.sleep(propagation)
        stage("symptom", **observe(problem))
        if args.healthy:
            stage("oracle_after_inject", result=oracle.evaluate())
            return
        # Expected failures: do not wait the full analysis grace for alerts that will not come.
        oracle.analysis_grace_seconds = 60
        stage("oracle_after_inject", result=oracle.evaluate())
        stage("oracle_after_blind_fixes", fixes=blind_fixes(problem), result=oracle.evaluate())
        oracle.analysis_grace_seconds = 900
        stage("operator_mitigation", variant=args.variant, **mitigate(problem, args.variant))
        stage("oracle_after_mitigation", result=poll(oracle, True, 1200))

        t = time.time()
        problem.recover_fault()
        stage("recover", seconds=round(time.time() - t))
        stage("oracle_after_recover", result=oracle.evaluate())
    finally:
        stage("done")


if __name__ == "__main__":
    main()
