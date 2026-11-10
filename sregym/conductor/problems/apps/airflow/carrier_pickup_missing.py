"""Airflow: a routine scheduler rollout loses a finished run's DAG success callback for good.

Upstream defect (Apache Airflow 3.3.2, apache/airflow @ aa19d2db). Found by the
Specula verification tool (finding MC-11), not yet reported upstream; reproduced on
the real stack with CeleryExecutor, providers-celery 3.24.0 and a standalone
dag-processor. The bug files in ``apache/airflow:3.3.2``
(jobs/scheduler_job_runner.py, callbacks/database_callback_sink.py,
models/dagrun.py, dag_processing/manager.py, dag_processing/processor.py,
utils/session.py) are byte-identical to the tested commit.

``_schedule_all_dag_runs`` (scheduler_job_runner.py:2812-2827) runs
``DagRun.update_state`` for a batch of runs and commits their terminal states
(``guard.commit()``); the ``DagCallbackRequest`` that ``update_state`` returns for a
DAG with ``on_success_callback``/``on_failure_callback`` exists only in the
in-memory ``callback_tuples`` list. ``_do_scheduling`` (:1938-1951) inserts each one
afterwards through ``executor.send_callback`` -> ``DatabaseCallbackSink.send``, one
``create_session()`` commit per callback. A scheduler that dies in between
(SIGTERM from a rollout: ``_exit_gracefully`` -> ``sys.exit``, and ``create_session``
does not commit on ``SystemExit``; SIGKILL; a DB error) loses the request. Nothing
re-creates it: ``get_running_dag_runs_to_examine`` selects RUNNING runs only, the
run has no "callback pending" marker and there is no startup sweep. The
dag-processor never receives the request, so the callback never runs; nothing is
logged above INFO (the old scheduler logs ``Exiting gracefully upon receiving signal
15``).

Here: the shop deployment also runs ``carrier_manifest`` every five minutes
(:03, :08, ...). Each run puts the new shipments on a manifest and closes it; its
DAG-level ``on_success_callback`` (``carrier_desk.request_pickup``, run by the
dag-processor) posts the carrier pickup request to ``ops-notify``
(carrier-pickups channel), normally 1-10 s after the run ends. One run's success
lands on a routine scheduler rollout (Recreate strategy): the run is ``success``,
its manifest is closed, and no pickup is ever requested. The airflow-monitor
reports the closed manifest without a pickup request; later manifests are
requested normally.

Trigger and driver (deterministic). The incident is part of an ordinary
maintenance pass: the scheduler is rolled, then the dag-processor once the new
scheduler is up, and the api-server later during the ageing window. For the
scheduler, the conductor opens one psql session in the postgres pod that waits
server-side until the run's last task instance is ``success``, then takes
``LOCK TABLE callback IN SHARE MODE`` and waits until a backend queues behind it
with the run already committed ``success`` (the scheduler's callback INSERT, i.e.
the scheduler is inside the window). The conductor then SIGSTOPs the scheduler
process from its kind node, issues the routine
``kubectl rollout restart deployment/airflow-scheduler``, waits until the pod's
SIGTERM is pending on the process, rolls back its session (releasing the lock) and
SIGCONTs the process. The scheduler handles the SIGTERM before its INSERT commits
and exits gracefully, exactly as if the signal had arrived within the natural
1-15 ms window; the lock is held for about 1 s (the scheduler is stopped 0.2-0.3 s), and nothing of the driver
remains (no process, file, env var, label, log line; ``log_lock_waits`` is off).
If the scheduler is faster than the lock (the callback is already written) the
attempt is abandoned with no restart, and the next run is tried. The incident ages
25 min with the storefront, all shop pipelines and the other pickups running.

Evidence the agent can reach: the run's ``end_date`` and the old scheduler's
termination (deployment rollout, ReplicaSet/pod events, the new scheduler pod's
start, and in Loki the old pod's ``Exiting gracefully``) are about a second apart;
every other manifest's pickup request arrived seconds after its run ended; the
dag-processor (restarted about a minute after the incident) executed every other
callback and logged no failure for this one (``dag.callback_exceptions`` stays 0);
the source in the image shows the commit-then-send order and the RUNNING-only
examination. The dag-processor restart invites the wrong "the dag-processor lost
the claimed callback" story (a real but separate upstream defect); the timing
rules it out.

The shop deployment around the incident (storefront, SQS order intake, partner
settlement, ops-notify) keeps running normally. Nothing remains in the database to
reset: the callback request never reached it, the symptom is a closed manifest with
no pickup, and the repair is to perform the lost callback's effect.

Mitigation: request the pickup for the incident manifest exactly once, e.g. run
the DAG's ``carrier_desk.request_pickup`` for the incident run in an Airflow pod, or
post the same request to ``ops-notify``. A blind scheduler or dag-processor
restart sends nothing; clearing or re-running the run builds a new manifest (the
parcels are already on the old one) and still never requests the incident
manifest; requesting a pickup twice fails.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from sregym.conductor.oracles.apps.airflow.carrier_pickups import AirflowCarrierPickupOracle
from sregym.conductor.oracles.apps.airflow.diagnosis import MechanismGatedJudgeOracle
from sregym.conductor.oracles.apps.airflow.shop_orders import pod_snapshot
from sregym.conductor.problems.apps.airflow.node_driver import NodeShell, container_processes, signal_pending
from sregym.conductor.problems.base import Problem
from sregym.service.apps.airflow.application import AirflowApplication
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.sregym.problems.airflow")

ROOT_CAUSE = (
    "carrier_manifest run {run} closed carrier manifest {manifest} ({parcels} parcels) and was marked success, but "
    "its DAG-level on_success_callback (carrier_desk.request_pickup, which the dag-processor runs and which posts the "
    "carrier pickup request to ops-notify) never ran, so no pickup was ever requested for manifest {manifest}. "
    "Cause: a routine rolling restart of the airflow-scheduler Deployment terminated the scheduler (SIGTERM -> "
    "'Exiting gracefully upon receiving signal 15' -> sys.exit) about a second after the run finished, in the window "
    "after the scheduler loop had committed the run's terminal SUCCESS state and before it had written the run's DAG "
    "callback request (DagCallbackRequest) to the callback table. Upstream Airflow 3.3.2 defect "
    "(scheduler_job_runner.py): _schedule_all_dag_runs commits the batch's terminal DagRun states (guard.commit()) "
    "while the callback requests produced by DagRun.update_state exist only in the scheduler's memory "
    "(callback_tuples); _do_scheduling inserts them afterwards, each in its own transaction "
    "(executor.send_callback -> DatabaseCallbackSink.send). The terminated scheduler took the request with it, and "
    "nothing re-creates it: the replacement scheduler examines only running DAG runs "
    "(get_running_dag_runs_to_examine), the run is already success, and there is no persisted 'callback pending' "
    "marker or startup sweep. The dag-processor never received a callback request for the run; it was healthy and "
    "ran every other run's callback within seconds. Nothing logged a warning or an error. Fix: request the pickup "
    "for manifest {manifest} once (run the DAG's request_pickup for run {run}, or post the same pickup request to "
    "ops-notify), without re-running the DAG run (a re-run builds a new manifest and never requests the old one). "
    "Grading requirements. Required for fault characterization (D2-Q1 and D2-Q3), the core: (1) the success "
    "callback never ran because its callback request was never created in the database / never handed to the "
    "dag-processor: it was lost on the scheduler side, not by the dag-processor, the callback code or ops-notify; "
    "(2) the scheduler was terminated by the rollout right after it had marked the run success and before it "
    "persisted/sent the callback request (the run state commit and the callback hand-off are separate steps, the "
    "request was held only in the old scheduler's memory); (3) nothing re-creates it afterwards (the new scheduler "
    "does not revisit runs that are already finished), so the loss is permanent. Observable equivalents are "
    "acceptable, for example 'the scheduler rollout hit the moment the run finished; the scheduler only queues the "
    "DAG callback after committing the success, so the request died with the old scheduler, and the new one never "
    "looks at finished runs again'. Exact code detail (guard.commit() in _schedule_all_dag_runs, the per-callback "
    "create_session in DatabaseCallbackSink.send, get_running_dag_runs_to_examine selecting RUNNING runs only, "
    "SystemExit skipping the commit) earns extra credit (D2-Q2) but is not required. Localization (D1): the "
    "airflow-scheduler (deployment/airflow-scheduler, its DAG callback hand-off after the run's state commit), "
    "together with the rollout that terminated it, is the correct fault origin; naming the rollout as the trigger "
    "is correct. The dag-processor, ops-notify, the carrier_desk callback code, the carrier_manifest tasks, the "
    "Celery worker, Redis, PgBouncer and PostgreSQL are not. Wrong mechanisms the evidence invites, each of which "
    "must get No for D2-Q1, D2-Q2 and D2-Q3: (a) the dag-processor lost the callback, e.g. it fetched (deleted) the "
    "callback row and was restarted before running it, or it was down or restarting when the callback was due "
    "(wrong: the dag-processor restart came minutes later, every other callback was picked up within "
    "seconds, and no callback row was ever written for this run); (b) the callback ran and failed or timed out "
    "(ops-notify unreachable, a database error, a bug in request_pickup; wrong: it never ran, nothing was logged "
    "and dag.callback_exceptions did not move); (c) ops-notify received the request and lost it; (d) the run was "
    "marked success externally or by hand, or the DAG's callback was not registered or failed to parse; (e) two "
    "schedulers raced or deduplicated the callback (the Recreate strategy never runs two); (f) generic 'Airflow "
    "callbacks are best-effort and not retried' without the scheduler-side loss. A diagnosis that only says 'the "
    "success callback did not run' or 'the callback was lost during the maintenance restarts', without attributing "
    "the loss to the scheduler being terminated after committing the run's success and before persisting the "
    "callback request, does not identify the mechanism: answer No to D2-Q1."
)


class AirflowCarrierPickupMissing(Problem):
    #: Manifest runs to try (a run whose callback is already written goes through normally).
    MAX_ATTEMPTS = 4
    ROUTINE_GAP_S = 30
    #: Seconds the conductor waits for the scheduler to reach the callback INSERT once the lock is held.
    BLOCK_WAIT_S = 30

    def __init__(self):
        super().__init__(app=AirflowApplication(manifests=True))
        self.kubectl = self.app.kubectl
        self.faulty_service = self.app.SCHEDULER_DEPLOY
        self.propagation_duration_s = 1500
        self.incident_run_id: str | None = None
        self.incident_manifest: str | None = None
        self.incident_parcels: int | None = None
        self.handover_snapshot: dict | None = None
        self._stopped: tuple[NodeShell, int] | None = None
        self._lock_session: subprocess.Popen | None = None
        self.root_cause = self._root_cause("(the run)", "(its manifest)", "(n)")
        self.diagnosis_oracle = MechanismGatedJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AirflowCarrierPickupOracle(problem=self)

    def _root_cause(self, run: str, manifest: str, parcels) -> str:
        return self.build_structured_root_cause(
            component="deployment/airflow-scheduler",
            namespace=self.namespace,
            description=ROOT_CAUSE.format(run=run, manifest=manifest, parcels=parcels),
        )

    # ------------------------------------------------------------------ helpers
    def _rollout_restart(self, kind: str, name: str, wait: bool = True) -> None:
        self.app.command("rollout", "restart", f"{kind}/{name}", timeout=60)
        if wait:
            self.app.command("rollout", "status", f"{kind}/{name}", "--timeout=600s", timeout=620)

    def _pickups(self, manifest) -> int:
        out = self.app.shop_psql(
            "SELECT count(*) FROM ops_notifications WHERE channel='carrier-pickups' "
            f"AND payload->>'manifest_id'='{manifest}'"
        ).strip()
        return int(out) if out.isdigit() else 0

    def _wait_pipelines_healthy(self, timeout: float = 1500) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            requested = self.app.shop_psql(
                "SELECT count(DISTINCT payload->>'manifest_id') FROM ops_notifications WHERE channel='carrier-pickups'"
            ).strip()
            if requested.isdigit() and int(requested) >= 2:
                return
            time.sleep(15)
        raise RuntimeError("carrier pickups are not being requested (fewer than two manifests requested)")

    def _next_manifest_run(self, timeout: float = 900) -> str:
        """The next scheduled carrier_manifest run, once its labels are being printed."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            run_id = self.app.psql(
                "SELECT r.run_id FROM dag_run r JOIN task_instance t ON t.dag_id = r.dag_id AND t.run_id = r.run_id "
                "WHERE r.dag_id='carrier_manifest' AND r.state='running' AND r.run_id LIKE 'scheduled__%' "
                "AND t.task_id='print_labels' AND t.state='running' ORDER BY r.queued_at DESC LIMIT 1"
            ).strip()
            if run_id:
                return run_id
            time.sleep(1)
        raise RuntimeError("no carrier_manifest run came up")

    def _scheduler_pid(self, shell: NodeShell, pod: str) -> int:
        for proc in container_processes(shell, pod, "scheduler"):
            if "python" in proc["args"] and " scheduler" in proc["args"]:
                return proc["pid"]
        raise RuntimeError(f"scheduler python process not found in {pod}")

    def _open_lock_session(self) -> subprocess.Popen:
        return subprocess.Popen(
            [
                "kubectl",
                "-n",
                self.namespace,
                "exec",
                "-i",
                self.app.POSTGRES_POD,
                "--",
                "psql",
                "-U",
                "airflow",
                "-d",
                "airflow",
                "-qtAX",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    @staticmethod
    def _read_marker(session: subprocess.Popen, marker: str, timeout: float) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = session.stdout.readline()
            if not line:
                raise RuntimeError("lock session ended early")
            if line.startswith(marker):
                return line.strip()
        raise TimeoutError(marker)

    def _end_lock_session(self) -> None:
        session, self._lock_session = self._lock_session, None
        if session is None:
            return
        try:
            session.stdin.write("ROLLBACK;\n\\q\n")
            session.stdin.flush()
            session.stdin.close()
            session.wait(timeout=30)
        except Exception:  # noqa: BLE001
            session.kill()

    # ------------------------------------------------------------------ injection
    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: routine maintenance rollout; scheduler restarted as a manifest run finishes ==")
        self._wait_pipelines_healthy()
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            if self._scheduler_rollout_in_callback_window(attempt):
                self.root_cause = self._root_cause(self.incident_run_id, self.incident_manifest, self.incident_parcels)
                self.diagnosis_oracle.expected = self.root_cause
                time.sleep(self.ROUTINE_GAP_S)
                print("[inject] routine rollout: dag-processor", flush=True)
                self._rollout_restart("deployment", "airflow-dag-processor")
                threading.Thread(target=self._later_routine_restarts, daemon=True).start()
                return
        raise RuntimeError(
            f"Could not land the scheduler rollout inside a callback window in {self.MAX_ATTEMPTS} tries"
        )

    def _scheduler_rollout_in_callback_window(self, attempt: int) -> bool:
        run_id = self._next_manifest_run()
        pod = self.app.scheduler_pod()
        shell = NodeShell(self.app.node_container(pod))
        restarted = False
        try:
            pid = self._scheduler_pid(shell, pod)
            run = f"dag_id='carrier_manifest' AND run_id='{run_id}'"
            self._lock_session = session = self._open_lock_session()
            session.stdin.write(
                "BEGIN;\n"
                "DO $$ BEGIN FOR i IN 1..60000 LOOP EXIT WHEN EXISTS (SELECT 1 FROM task_instance WHERE "
                f"{run} AND task_id='close_manifest' AND state='success'); PERFORM pg_sleep(0.004); END LOOP; END $$;\n"
                "LOCK TABLE callback IN SHARE MODE;\n"
                f"SELECT 'armed|' || coalesce((SELECT state FROM dag_run WHERE {run}), '');\n"
                f"DO $$ BEGIN FOR i IN 1..{self.BLOCK_WAIT_S * 100} LOOP EXIT WHEN EXISTS (SELECT 1 FROM pg_locks "
                "WHERE relation = 'callback'::regclass AND NOT granted); PERFORM pg_sleep(0.01); END LOOP; END $$;\n"
                "SELECT 'held|' || EXISTS (SELECT 1 FROM pg_locks WHERE relation = 'callback'::regclass "
                f"AND NOT granted) || '|' || coalesce((SELECT state FROM dag_run WHERE {run}), '');\n"
            )
            session.stdin.flush()
            print(f"[inject] attempt {attempt}: armed on {run_id}, scheduler {pod} pid {pid}", flush=True)
            armed = self._read_marker(session, "armed|", 600)
            held = self._read_marker(session, "held|", self.BLOCK_WAIT_S + 30)
            _, blocked, state = held.split("|")
            if blocked not in ("t", "true") or state != "success":
                self._end_lock_session()
                print(
                    f"[inject] attempt {attempt}: missed the window ({armed}, {held}); "
                    "that run's pickup goes out normally",
                    flush=True,
                )
                return False
            shell.send(f"kill -STOP {pid}")
            stopped_at = time.time()
            self._stopped = (shell, pid)
            try:
                self.app.command("rollout", "restart", "deployment/airflow-scheduler", timeout=60)
                restarted = True
                waited = time.monotonic()
                while time.monotonic() - waited < 90 and not signal_pending(shell, pid):
                    time.sleep(0.02)
            finally:
                self._end_lock_session()
                shell.run(f"kill -CONT {pid} 2>/dev/null || true")
                self._stopped = None
            print(
                f"[inject] scheduler held {time.time() - stopped_at:.2f}s at the callback INSERT of {run_id} "
                "until its SIGTERM",
                flush=True,
            )
        finally:
            self._end_lock_session()
            shell.close()
        if restarted:
            self.app.command("rollout", "status", "deployment/airflow-scheduler", "--timeout=600s", timeout=620)
        manifest = self.app.shop_psql(
            f"SELECT manifest_id || '|' || parcels FROM manifests WHERE dag_run_id='{run_id}' ORDER BY manifest_id DESC "
            "LIMIT 1"
        ).strip()
        # Confirm the loss: the replacement scheduler is up and no pickup was requested for the manifest.
        time.sleep(60)
        state = self.app.psql(
            f"SELECT state FROM dag_run WHERE dag_id='carrier_manifest' AND run_id='{run_id}'"
        ).strip()
        manifest_id, parcels = (manifest.split("|") + [""])[:2]
        requests = self._pickups(manifest_id) if manifest_id else -1
        print(
            f"[inject] 60 s after the rollout: run state={state}, manifest={manifest}, pickup requests={requests}",
            flush=True,
        )
        if state != "success" or requests != 0 or not parcels.isdigit() or int(parcels) == 0:
            return False
        self.incident_run_id = run_id
        self.incident_manifest = manifest_id
        self.incident_parcels = int(parcels)
        return True

    def _later_routine_restarts(self) -> None:
        """The rest of the maintenance pass, during the ageing window; then record the handover state."""
        time.sleep(240)
        try:
            self._rollout_restart("deployment", "airflow-api-server")
        except subprocess.CalledProcessError as error:
            logger.warning("routine restart of the api-server failed: %s", error)
        try:
            self.handover_snapshot = pod_snapshot(self.app)
        except Exception as error:  # noqa: BLE001
            logger.warning("could not record the handover snapshot: %s", error)

    # ------------------------------------------------------------------ reference repair
    def reference_mitigation(self) -> str:
        """Run the DAG's own pickup request for the incident run, once."""
        if not self.incident_run_id:
            return "no incident run recorded"
        if self._pickups(self.incident_manifest):
            return f"pickup already requested for manifest {self.incident_manifest}"
        return self.app.command(
            "exec",
            "deploy/airflow-dag-processor",
            "-c",
            "dag-processor",
            "--",
            "python",
            "-c",
            "import sys; sys.path.insert(0, '/opt/airflow/dags'); from carrier_desk import request_pickup; "
            "request_pickup({'run_id': sys.argv[1]})",
            self.incident_run_id,
            timeout=120,
        ).strip()

    # ------------------------------------------------------------------ recovery
    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: request the lost pickup; make sure no scheduler is left stopped ==")
        self._end_lock_session()
        if self._stopped:
            shell, pid = self._stopped
            try:
                shell.run(f"kill -CONT {pid} 2>/dev/null || true")
            finally:
                self._stopped = None
        try:
            print(f"[recover] {self.reference_mitigation()}")
        except Exception as error:  # noqa: BLE001
            print(f"[recover] pickup request failed: {error}")
