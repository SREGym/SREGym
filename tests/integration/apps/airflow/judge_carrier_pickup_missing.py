"""Judge calibration for airflow_carrier_pickup_missing (mechanism-gated judge).

Feeds the problem's diagnosis judge a correct, a correct-but-terse, plausible-but-wrong
and vague diagnoses, each REPEATS times, and checks that every correct one passes
and every wrong one fails. Needs judge model credentials, e.g.

    JUDGE_MODEL_ID=anthropic/claude-opus-5-5 ANTHROPIC_API_KEY=... \
        python tests/integration/apps/airflow/judge_carrier_pickup_missing.py [out.json]
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

os.environ.setdefault("JUDGE_MODEL_ID", "anthropic/claude-opus-5-5")

REPEATS = 2
RUN = "scheduled__2026-11-10T07:08:00+00:00"

CASES = {
    "correct": (
        True,
        f"Manifest 4107 (9 parcels) was closed by carrier_manifest run {RUN}, which ended success at 07:13:41.2, but "
        "no carrier pickup was ever requested for it; every other manifest got its request on carrier-pickups 2-4 s "
        "after its run ended. The request is sent by the DAG's on_success_callback (carrier_desk.request_pickup), "
        "executed by the dag-processor from a DagCallbackRequest row in the callback table. The dag-processor logs "
        "no execution and no failure for that run (dag.callback_exceptions is 0), so the callback never ran. At "
        "07:13:42 the airflow-scheduler Deployment was rolled (kubectl rollout restart, Recreate): the old scheduler "
        "got SIGTERM about a second after it committed the run's success. In Airflow 3.3.2 _do_scheduling commits "
        "the terminal DagRun states first (_schedule_all_dag_runs -> guard.commit()) and only afterwards inserts the "
        "DAG callback requests one by one through DatabaseCallbackSink.send, so the request was only in the old "
        "scheduler's memory when it exited (sys.exit in _exit_gracefully, nothing committed). The new scheduler only "
        "examines running DAG runs, so nothing ever recreated the callback. The dag-processor restart at 07:14:40 is "
        "unrelated: it came a minute later. Fix: ran carrier_desk.request_pickup for the run once; the pickup for "
        "manifest 4107 is requested.",
    ),
    "correct_terse": (
        True,
        "The scheduler rollout hit the second the 07:08 carrier_manifest run finished: the scheduler had already "
        "marked the run success but had not yet queued its DAG success callback, which it does only after that "
        "commit, so the callback request died with the old scheduler and the new one never looks at finished runs "
        "again. The pickup for manifest 4107 was never requested; I requested it once by hand.",
    ),
    "wrong_dag_processor": (
        False,
        f"The pickup for manifest 4107 (run {RUN}) was never requested because the dag-processor lost the success "
        "callback. The scheduler wrote the DagCallbackRequest to the callback table as usual; the dag-processor "
        "fetched it (_fetch_callbacks_from_db deletes the row when claiming it) and queued it in memory, but the "
        "routine rollout restarted the dag-processor before it ran the callback file, so the claimed callback was "
        "lost with the old dag-processor pod - a known Airflow issue where claimed callbacks are not durable. The "
        "scheduler restart is a red herring: the run had already finished. Fix: requested the pickup for 4107 by hand.",
    ),
    "wrong_callback_failed": (
        False,
        f"Manifest 4107's pickup was never requested: the on_success_callback of run {RUN} ran during the maintenance "
        "restarts and its POST to ops-notify failed (the cluster was rolling pods and the request timed out), and "
        "Airflow never retries a failed DAG callback, so the request was dropped. The run itself succeeded. Fix: "
        "re-sent the pickup request for manifest 4107 and suggest adding retries to request_pickup.",
    ),
    "wrong_scheduler_race": (
        False,
        "During the scheduler rollout two schedulers briefly ran at the same time; both examined the carrier_manifest "
        "run when it finished and the callback was deduplicated / skipped because the other scheduler had marked the "
        "run first, so no on_success_callback was sent. Fix: requested the pickup for manifest 4107 by hand.",
    ),
    "vague": (
        False,
        "The on_success_callback for the 07:08 carrier_manifest run never ran, apparently lost during the maintenance "
        "restarts around 07:13, so manifest 4107 never got its pickup request. Requested it by hand.",
    ),
}


def main():
    from sregym.conductor.problems.apps.airflow.carrier_pickup_missing import AirflowCarrierPickupMissing

    problem = AirflowCarrierPickupMissing()
    problem.root_cause = problem._root_cause(RUN, "4107", 9)
    oracle = problem.diagnosis_oracle
    oracle.expected = problem.root_cause
    results, ok = {}, True
    for name, (should_pass, text) in CASES.items():
        runs = []
        for attempt in range(REPEATS):
            report = oracle.evaluate(text)
            runs.append(
                {
                    "success": report.get("success"),
                    "accuracy": report.get("accuracy"),
                    "required_items": report.get("required_items"),
                    "gated_by": report.get("gated_by"),
                    "dimensions": {k: v["score"] for k, v in (report.get("dimensions") or {}).items()},
                    "checklist": report.get("checklist"),
                }
            )
            print(
                f"### {name} try{attempt + 1}: success={report.get('success')} accuracy={report.get('accuracy')} "
                f"required={report.get('required_items')} dims={runs[-1]['dimensions']}",
                flush=True,
            )
        results[name] = {"expected_pass": should_pass, "runs": runs}
        ok &= all(r["success"] is should_pass for r in runs)
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w") as handle:
            json.dump(results, handle, indent=1, default=str)
    summary = {k: [r["success"] for r in v["runs"]] for k, v in results.items()}
    print("SUMMARY", json.dumps(summary), "calibrated" if ok else "NOT CALIBRATED", flush=True)


if __name__ == "__main__":
    main()
