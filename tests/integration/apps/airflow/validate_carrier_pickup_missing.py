"""Full lifecycle validation for airflow_carrier_pickup_missing.

deploy (conductor path) -> image check -> healthy oracle PASSES and the monitor is
healthy -> inject -> driver footprint check -> age -> symptom through the monitor
and ordinary evidence -> oracle FAILS -> blind scheduler + dag-processor restart ->
oracle STILL FAILS -> optional blind clear of the run (--clear) -> oracle STILL
FAILS -> repair (--fix reference|manual) -> oracle PASSES -> optional extra case ->
recover_fault -> cleanup.

    python tests/integration/apps/airflow/validate_carrier_pickup_missing.py OUT.json
        [--fix reference|manual] [--age S] [--clear] [--extra restart_after_fix|request_twice]
        [--healthy-only] [--keep] [--reuse] [--resume-incident RUN,MANIFEST,PARCELS]

Needs a kind cluster (the driver signals the scheduler process from its kind node), kubectl,
helm and docker; run from the repository root, e.g. inside the DinD container.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from logger import init_logger  # noqa: E402
from sregym.conductor.conductor import Conductor, ConductorConfig  # noqa: E402

PROBLEM = "airflow_carrier_pickup_missing"
out_path = sys.argv[1]
args = sys.argv[2:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


FIX = opt("--fix", "reference")
AGE = int(opt("--age", "1500"))
EXTRA = opt("--extra")
CLEAR = "--clear" in args
HEALTHY_ONLY = "--healthy-only" in args
record = {"problem": PROBLEM, "fix": FIX, "age_s": AGE, "extra": EXTRA, "clear": CLEAR, "stages": {}}
started = time.time()

#: Files of the defect; their sha256 inside the image is recorded so it can be compared with the tested commit.
BUG_FILES = {
    "jobs/scheduler_job_runner.py": None,
    "callbacks/database_callback_sink.py": None,
    "models/dagrun.py": None,
    "dag_processing/manager.py": None,
    "dag_processing/processor.py": None,
    "utils/session.py": None,
}


def stage(name, **data):
    data["t"] = round(time.time() - started, 1)
    record["stages"][name] = data
    print(f"\n===== {name}: {json.dumps(data, default=str)[:4000]}", flush=True)
    with open(out_path, "w") as handle:
        json.dump(record, handle, indent=1, default=str)


def sh(*cmd, timeout=120):
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout).stdout


def verdict(result):
    return {k: result.get(k) for k in ("success", "reason", "collateral")} | {
        "checks": {k: v.get("ok") for k, v in (result.get("checks") or {}).items()},
        "details": result.get("checks"),
    }


def footprint():
    """Anything of the driver left behind: stopped processes on the nodes, waiting SQL, lock holders."""
    left = {}
    for node in sh("kind", "get", "nodes").split():
        rows = sh("docker", "exec", node, "sh", "-c", "ps -e -o stat=,pid=,args= | awk '$1 ~ /^T/' | grep -v awk")
        if rows.strip():
            left[node] = rows.strip()
    left["waiting_sql"] = app.psql(
        "SELECT count(*) FROM pg_stat_activity WHERE (query LIKE 'DO $$%' OR query LIKE 'LOCK%') "
        "AND pid <> pg_backend_pid()"
    ).strip()
    left["callback_locks"] = app.psql(
        "SELECT count(*) FROM pg_locks WHERE relation = 'callback'::regclass AND mode = 'ShareLock'"
    ).strip()
    left["events"] = sh(
        "kubectl",
        "-n",
        "airflow",
        "get",
        "events",
        "--sort-by=.lastTimestamp",
        "-o",
        "custom-columns=T:.lastTimestamp,R:.reason,O:.involvedObject.name,M:.message",
    )[-3500:]
    left["pods"] = sh("kubectl", "-n", "airflow", "get", "pods")
    return left


def evidence():
    run = problem.incident_run_id
    ev = {
        "run": app.psql(
            f"SELECT state, start_date, end_date FROM dag_run WHERE dag_id='carrier_manifest' AND run_id='{run}'"
        ).strip(),
        "callback_rows": app.psql(
            "SELECT type || '|' || coalesce(state,'') || '|' || created_at FROM callback "
            "ORDER BY created_at DESC LIMIT 5"
        ).strip(),
        # Pickup latency of every manifest run: run end -> pickup request received.
        "pickup_latency": [],
        "scheduler_pod_start": sh(
            "kubectl",
            "-n",
            "airflow",
            "get",
            "pods",
            "-l",
            "component=scheduler",
            "-o",
            "jsonpath={.items[*].metadata.creationTimestamp}",
        ),
        "dag_processor_pod_start": sh(
            "kubectl",
            "-n",
            "airflow",
            "get",
            "pods",
            "-l",
            "component=dag-processor",
            "-o",
            "jsonpath={.items[*].metadata.creationTimestamp}",
        ),
    }
    runs = app.psql(
        "SELECT run_id || '|' || to_char(end_date AT TIME ZONE 'UTC', 'HH24:MI:SS.MS') FROM dag_run "
        "WHERE dag_id='carrier_manifest' AND state='success' ORDER BY end_date"
    ).split()
    for row in runs:
        run_id, end = row.split("|")
        got = app.shop_psql(
            "SELECT to_char(min(n.received_at) AT TIME ZONE 'UTC', 'HH24:MI:SS.MS') FROM ops_notifications n "
            f"WHERE n.channel='carrier-pickups' AND n.run_id='{run_id}'"
        ).strip()
        ev["pickup_latency"].append(f"{run_id} end={end} pickup={got or 'NONE'}")
    ev["callback_exceptions_metric"] = sh(
        "kubectl",
        "-n",
        "airflow",
        "exec",
        "deploy/airflow-monitor",
        "--",
        "python",
        "-c",
        "import urllib.request;print('\\n'.join(l for l in urllib.request.urlopen("
        "'http://airflow-statsd:9102/metrics',timeout=20).read().decode().splitlines() if 'callback' in l)[:3000])",
    )
    ev["dag_processor_callback_log"] = sh(
        "kubectl",
        "-n",
        "airflow",
        "exec",
        "deploy/airflow-dag-processor",
        "-c",
        "dag-processor",
        "--",
        "sh",
        "-c",
        "grep -rh 'dag callback\\|pickup requested\\|Callback failed' /opt/airflow/logs/dag_processor | tail -20",
    )[-3000:]
    return ev


def image_check():
    """sha256 of the defect files inside the deployed image."""
    script = (
        "import hashlib,airflow,os;b=os.path.dirname(airflow.__file__);"
        "print(airflow.__version__);"
        f"[print(f, hashlib.sha256(open(os.path.join(b,f),'rb').read()).hexdigest()) for f in {list(BUG_FILES)}]"
    )
    return app.command("exec", "deploy/airflow-scheduler", "-c", "scheduler", "--", "python", "-c", script).strip()


init_logger()
conductor = Conductor(config=ConductorConfig(deploy_loki=False))
conductor.problem_id = PROBLEM
problem = conductor.problems.get_problem_instance(PROBLEM)
conductor.problem, conductor.app = problem, problem.app
oracle = problem.mitigation_oracle
app = problem.app
try:
    conductor.dependency_check(["kubectl", "helm", "docker"])
    conductor.fix_kubernetes()
    t = time.time()
    if "--reuse" not in args:
        conductor.undeploy_app()
        conductor.deploy_app()
    stage("deploy", seconds=round(time.time() - t), image=image_check())

    resume = opt("--resume-incident")
    if resume:
        # Continue a validation whose incident is already in place: RUN,MANIFEST,PARCELS.
        problem.incident_run_id, problem.incident_manifest, parcels = resume.split(",")
        problem.incident_parcels = int(parcels)
        problem.handover_snapshot = __import__(
            "sregym.conductor.oracles.apps.airflow.shop_orders", fromlist=["pod_snapshot"]
        ).pod_snapshot(app)
        AGE = 0
    problem._wait_pipelines_healthy()
    healthy = oracle.evaluate() if not resume else {"success": True, "note": "resumed"}
    stage(
        "healthy_oracle",
        result=verdict(healthy) if not resume else healthy,
        monitor=app.monitor_status(),
        pickups=app.shop_psql(
            "SELECT id, received_at, run_id, payload->>'manifest_id', payload->>'parcels' "
            "FROM ops_notifications WHERE channel='carrier-pickups' ORDER BY id"
        ),
    )
    if healthy.get("success") is not True:
        raise SystemExit("healthy deployment did not pass the oracle")
    if HEALTHY_ONLY:
        record["passed"] = True
        raise SystemExit(0)

    t = time.time()
    if not resume:
        problem.inject_fault()
    stage(
        "inject",
        seconds=round(time.time() - t),
        run=problem.incident_run_id,
        manifest=problem.incident_manifest,
        parcels=problem.incident_parcels,
    )
    stage("footprint_after_inject", **footprint())

    time.sleep(AGE)
    stage("symptom", monitor=app.monitor_status(), evidence=evidence(), footprint=footprint())

    result = oracle.evaluate()
    stage("oracle_after_inject", result=verdict(result))
    if result.get("success") is not False:
        raise SystemExit("oracle did not fail after injection")

    for name in ("airflow-scheduler", "airflow-dag-processor"):
        app.command("rollout", "restart", f"deployment/{name}")
        app.command("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=320)
    time.sleep(60)
    result = oracle.evaluate()
    stage("oracle_after_blind_restart", result=verdict(result))
    if result.get("success") is not False:
        raise SystemExit("blind restart unexpectedly passed")

    if CLEAR:
        logical = app.psql(
            "SELECT to_char(logical_date AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS+00:00') FROM dag_run "
            f"WHERE dag_id='carrier_manifest' AND run_id='{problem.incident_run_id}'"
        ).strip()
        out = app.command(
            "exec",
            "deploy/airflow-api-server",
            "-c",
            "api-server",
            "--",
            "airflow",
            "tasks",
            "clear",
            "carrier_manifest",
            "-s",
            logical,
            "-e",
            logical,
            "-y",
            timeout=180,
        )
        time.sleep(150)
        result = oracle.evaluate()
        stage(
            "oracle_after_blind_clear",
            cleared=out[-800:],
            result=verdict(result),
            manifests=app.shop_psql(
                f"SELECT manifest_id, parcels, closed_at FROM manifests WHERE dag_run_id='{problem.incident_run_id}'"
            ),
        )
        if result.get("success") is not False:
            raise SystemExit("blind clear unexpectedly passed")

    if FIX == "reference":
        done = problem.reference_mitigation()
    else:
        payload = json.dumps(
            {
                "channel": "carrier-pickups",
                "manifest_id": int(problem.incident_manifest),
                "parcels": problem.incident_parcels,
                "text": f"pickup requested for manifest {problem.incident_manifest} (on-call)",
            }
        )
        done = app.command(
            "exec",
            "deploy/storefront",
            "--",
            "python",
            "-c",
            "import sys,urllib.request;print(urllib.request.urlopen(urllib.request.Request("
            "'http://ops-notify:8080/notify',data=sys.argv[1].encode(),"
            "headers={'Content-Type':'application/json'},method='POST'),timeout=10).read().decode())",
            payload,
        )
    stage("repair", fix=FIX, done=done)
    result = oracle.evaluate()
    stage("oracle_after_repair", result=verdict(result))
    if result.get("success") is not True:
        raise SystemExit("oracle did not pass after the repair")

    if EXTRA == "restart_after_fix":
        # Harmless heavier step after the correct fix: must still pass, reported as collateral.
        app.command("rollout", "restart", "deployment/airflow-scheduler")
        app.command("rollout", "status", "deployment/airflow-scheduler", "--timeout=300s", timeout=320)
        time.sleep(20)
        result = oracle.evaluate()
        stage("oracle_after_restart_after_fix", result=verdict(result))
        record["extra_ok"] = result.get("success") is True and "scheduler" in (
            result.get("collateral", {}).get("restarted_since_handover") or []
        )
    elif EXTRA == "request_twice":
        # Wrong repair: request the pickup again. Two trucks for one manifest must fail.
        app.command(
            "exec",
            "deploy/airflow-dag-processor",
            "-c",
            "dag-processor",
            "--",
            "python",
            "-c",
            "import sys; sys.path.insert(0, '/opt/airflow/dags'); from carrier_desk import request_pickup; "
            "request_pickup({'run_id': sys.argv[1]})",
            problem.incident_run_id,
            timeout=120,
        )
        result = oracle.evaluate()
        stage("oracle_after_request_twice", result=verdict(result))
        record["extra_ok"] = result.get("success") is False and result.get("reason") == "pickup_requested_twice"

    if EXTRA != "request_twice":
        t = time.time()
        problem.recover_fault()
        result = oracle.evaluate()
        stage("recover", seconds=round(time.time() - t), result=verdict(result))
        if result.get("success") is not True:
            raise SystemExit("oracle did not pass after recover_fault")
    record["passed"] = True if EXTRA is None else bool(record.get("extra_ok"))
finally:
    record["elapsed_s"] = round(time.time() - started)
    if "--keep" not in args:
        try:
            conductor.problem.app.cleanup()
        except Exception as error:  # noqa: BLE001
            record["cleanup_error"] = str(error)
    with open(out_path, "w") as handle:
        json.dump(record, handle, indent=1, default=str)
    print(f"\nRESULT passed={record.get('passed', False)} elapsed={record['elapsed_s']}s", flush=True)
