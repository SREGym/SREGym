"""A task-worker rollout during a team's release reopens the merged release branch.

Upstream bug (Infrahub 1.11.9, unfixed and unpublished at the time of writing;
found and reproduced by Specula as CR-13): ``initialization()``
(``backend/infrahub/core/initialization.py``), which every task-worker and API
process runs at start, loads every Branch object once (``Branch.get_list``), then
loads each branch's schema from Neo4j one by one: about 3 s for every branch
whose schema differs from main's. When a recomputed schema hash differs from the
hash on the Branch object it loaded at the start, it saves that object
(``await branch.save(db=db)``) with no lock and no re-read. ``Branch.save`` runs
``StandardNodeUpdateQuery`` (``SET n = $node_prop``), which overwrites every
field of the branch vertex, so anything that changed the branch while the
process was starting is reverted. A branch that got a schema change and was
merged in that window goes from MERGED back to OPEN (``merge_started_at`` NULL),
although its changes are on main.

The natural event: the platform team rolls out the task-workers (an ordinary
``kubectl rollout restart``) while the lifecycle team's release pipeline loads
the rest of its schema change into its branch ``lifecycle-eol`` and merges it.
The deployment's history makes worker start-up slow, as it is in long-lived
deployments: 40 older planning branches and the teams' branches all trail main's
schema since the asset team's later release on main, so a starting worker
imports about 43 branch schemas (~130 s). The conductor runs the rollout and
starts the pipeline from the client pod as soon as the new worker has read the
branch list (it follows that pod's log from outside the cluster), then records
the release in inventory-sync's release plan, as the pipeline's last step.
Nothing is injected into Infrahub, Redis or Neo4j. A routine task-worker rollout
some minutes earlier (which also tells the conductor where lifecycle-eol sits in
a worker's start-up order) gives the cluster comparable pod events. When the
rollout does not reopen the branch, the namespace is redeployed from scratch.
The incident ages 25 minutes before handover.

The agent gets the plain application description only. The symptom shows in
inventory-sync's log: every minute its release check finds lifecycle-eol OPEN,
merges it again, and the merge task fails (the task-worker logs a
MergeConstraintsViolatedError on SchemaAttribute: the schema it re-applies is
already on main) and rolls back; main is briefly write-blocked by each attempt.

Correct repair: recognize that lifecycle-eol was merged (its attributes and
devices are on main; the restarted worker logged "New schema detected" for it
right after the merge) and make it not OPEN: set its status back to MERGED in
the database, or delete the branch now that its changes are on main; keep the
release on main and in the release plan. Re-merging, rebasing, recovering or
restarting does not help.
"""

import json
import logging
import select
import subprocess
import threading
import time

from sregym.conductor.oracles.apps.infrahub.reopened_release import InfrahubReopenedReleaseOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.infrahub.application import InfrahubApplication
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.sregym.problems.infrahub")

RELEASE = "lifecycle-eol"
#: The lifecycle team's schema change: loaded into its branch earlier, and the rest at release time.
PART_ONE, PART_TWO = ["eol_date"], ["eol_date", "support_end"]
TEAMS = {
    "security-zones": {"attrs": ["security_zone"], "prefix": "sz", "devices": 2, "merge": True},
    "ipam-pools": {"attrs": ["mgmt_pool"], "prefix": "pool", "devices": 2},
    RELEASE: {"attrs": PART_ONE, "prefix": "eol", "devices": 3, "after_release": True},
}
RELEASE_DEVICES = [f"eol-{i:02d}" for i in range(3)]
HISTORY_BRANCHES = 40
MAIN_RELEASE = ["asset_tag"]
#: The release branch must come this late in a starting worker's branch imports (its pipeline needs ~60 s).
MIN_LEAD_SECONDS = 90
MAX_ATTEMPTS = 3
#: The routine task-worker rollout happens this long before the release (roughly).
ROLLOUT_LEAD_S = 8 * 60
PROPAGATION_S = 25 * 60

ROOT_CAUSE = (
    "The lifecycle team's release pipeline loaded the rest of its schema change into branch lifecycle-eol and "
    "merged it into main (MERGED; its eol_date and support_end attributes and its devices are on main) while the "
    "task-workers were being rolled out. One new task-worker process was still starting: Infrahub's start-up "
    "initialization (core/initialization.py) had read every Branch object before the release, and then imported "
    "each branch's schema one by one (slow, because about 43 older branches trail main's schema after the asset "
    "team's release). When it reached lifecycle-eol it saw a schema hash different from the one on the Branch "
    "object it had read at start (the release had changed it), logged 'New schema detected after pulling the schema "
    "from the db' for lifecycle-eol, and saved its stale copy of the whole Branch object with no lock or re-read "
    "(Branch.save, SET n = $node_prop). That upstream Infrahub 1.11.9 defect reverted the branch from MERGED to "
    "OPEN (merge_started_at NULL) although its changes are on main. Since then inventory-sync's release plan check "
    "finds lifecycle-eol OPEN every minute and merges it again; each attempt fails with "
    "MergeConstraintsViolatedError (SchemaAttribute count constraint, because the schema it re-applies is already "
    "on main), rolls back and briefly blocks writes to main. Correct repair: confirm the release is on main and make "
    "lifecycle-eol not OPEN (set its status back to MERGED, or delete the branch since its changes are merged), "
    "keeping the release on main and in inventory-sync's release plan. "
    "Grading requirements: for D1, the faulty component is Infrahub's start-up initialization in the restarted "
    "task-worker (a diagnosis naming that task-worker process, or Infrahub's worker start-up, localizes correctly; "
    "one blaming inventory-sync, Neo4j or Redis does not). D2 has a required core and a deeper detail. The core, "
    "required for D2-Q1, is that a task-worker that was starting during the release overwrote the merged branch "
    "with a stale copy of its Branch record read before the merge, which reverted MERGED to OPEN; accept "
    "observable equivalents such as 'the new task-worker's start-up schema-hash fix-up saved an old version of "
    "lifecycle-eol (logged New schema detected for it right after the merge), resetting its status to OPEN'. The "
    "detail, which adds credit for D2-Q2 but is not required, is the code-level account (initialization() reads "
    "the branches once, then saves the stale object after the slow per-branch schema import, with a full-object "
    "SET and no lock) and why the start-up was slow. Answer No to D2-Q1, D2-Q2 and D2-Q3 for any of these wrong "
    "mechanisms the evidence invites: the first merge failed or was rolled back (it completed; its changes are on "
    "main); the merge was never done; inventory-sync's repeated merges or a concurrent merge reverted it; the "
    "rollback of a failed re-merge reopened it (it was OPEN before the first re-merge); a schema conflict or "
    "data-constraint problem in the release itself; Redis, the cache, or lost locks; or the rollout killing the "
    "merge mid-flight."
)


class InfrahubWorkerRestartReopensRelease(Problem):
    def __init__(self):
        app = InfrahubApplication()
        app.history_branches = HISTORY_BRANCHES
        app.team_branches = {b: dict(t) for b, t in TEAMS.items()}
        app.main_release = list(MAIN_RELEASE)
        app.inventory_releases = ["security-zones"]
        super().__init__(app)
        self.kubectl = app.kubectl
        self.faulty_service = "task-worker"
        self.observed = {}
        self.release_started = False
        self.propagation_duration_s = PROPAGATION_S
        self.root_cause = self.build_structured_root_cause(
            component="task-worker", namespace=self.namespace, description=ROOT_CAUSE
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = InfrahubReopenedReleaseOracle(self, RELEASE, PART_TWO, RELEASE_DEVICES)
        app.describe()

    # ------------------------------------------------------------------ helpers
    def _rollout_workers(self):
        """Restart the task-workers; return the names of the pods that existed before."""
        old = {p["metadata"]["name"] for p in self.app.pods("task-worker")}
        self.app.command("rollout", "restart", "deployment/task-worker")
        return old

    def _first_new_pod(self, old, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for pod in self.app.pods("task-worker"):
                statuses = pod["status"].get("containerStatuses") or [{}]
                if pod["metadata"]["name"] not in old and statuses[0].get("state", {}).get("running"):
                    return pod["metadata"]["name"]
            time.sleep(0.5)
        raise RuntimeError("No new task-worker pod started")

    def _import_order(self, pod):
        """[(seconds after the first import, branch)] from a worker's start-up log."""
        order, first = [], None
        for line in self.app.logs(pod).splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") != "Importing schema":
                continue
            stamp = time.mktime(time.strptime(event["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
            first = first if first is not None else stamp
            order.append((round(stamp - first, 1), event.get("branch")))
        return order

    def _routine_rollout(self):
        """A routine task-worker rollout; returns how long after its first import the release branch came."""
        old = self._rollout_workers()
        pod = self._first_new_pod(old)
        self.app.rollout("task-worker", timeout=900)
        order = self._import_order(pod)
        lead = next((t for t, b in order if b == RELEASE), None)
        return {"pod": pod, "imports": len(order), "release_lead_seconds": lead}

    def _state(self):
        rows = self.app.cypher(f"MATCH (b:Branch {{name: '{RELEASE}'}}) RETURN b.status, b.merge_started_at")
        return rows[0] if rows else None

    def _reopen_during_rollout(self, record):
        app = self.app
        old = self._rollout_workers()
        pod = self._first_new_pod(old)
        proc = subprocess.Popen(
            ["kubectl", "-n", self.namespace, "logs", "-f", pod],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        pipeline = {}
        try:
            deadline = time.monotonic() + 600
            started = saved = None
            while time.monotonic() < deadline and saved is None:
                ready, _, _ = select.select([proc.stdout], [], [], 1)
                if not ready:
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                if started is None and '"Importing schema"' in line:
                    # The new worker has read the branch list: the release pipeline runs now.
                    started = time.time()
                    thread = threading.Thread(
                        target=lambda: pipeline.update(
                            app.run_client(
                                "release_pipeline", branch=RELEASE, attrs=PART_TWO, timeout=300, exec_timeout=600
                            )
                        )
                    )
                    thread.start()
                if "New schema detected" in line and f'"branch": "{RELEASE}"' in line:
                    saved = time.time()
                if "Worker initialization completed" in line:
                    break
            if started is not None:
                thread.join()
        finally:
            proc.kill()
            proc.wait()
        record.update(worker_pod=pod, pipeline=pipeline)
        if started is not None and saved is not None:
            record["stale_save_after_pipeline_start_s"] = round(saved - started, 1)
        app.rollout("task-worker", timeout=900)
        return pipeline.get("merged") and saved is not None

    def _wait_failed_release_merge(self, timeout=420):
        """inventory-sync's release check merges the reopened branch again and the merge fails."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            logs = self.app.command("logs", "deploy/inventory-sync", "--since=10m", timeout=60)
            for line in logs.splitlines():
                if '"op": "release"' in line and f'"branch": "{RELEASE}"' in line and '"ok": false' in line:
                    return json.loads(line)
            time.sleep(15)
        return None

    # ------------------------------------------------------------------ lifecycle
    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                app.redeploy()
                self.mitigation_oracle.capture_baseline()
            record = {}
            self.observed[f"attempt_{attempt}"] = record
            rolled_out = time.monotonic()
            record["routine_rollout"] = self._routine_rollout()
            lead = record["routine_rollout"]["release_lead_seconds"]
            if lead is None or lead < MIN_LEAD_SECONDS:
                record["outcome"] = f"release branch imported too early in a worker's start-up ({lead} s)"
                continue
            time.sleep(max(0.0, ROLLOUT_LEAD_S - (time.monotonic() - rolled_out)))
            self.release_started = True
            reopened = self._reopen_during_rollout(record)
            record["state"] = self._state()
            if not reopened or not record["state"] or not record["state"].startswith('"OPEN"'):
                record["outcome"] = "the release branch was not reopened"
                self.release_started = False
                continue
            # The pipeline's last step: the release goes on inventory-sync's release plan.
            app.set_inventory_releases(["security-zones", RELEASE])
            record["failed_release_merge"] = self._wait_failed_release_merge()
            if not record["failed_release_merge"]:
                record["outcome"] = "inventory-sync never retried the reopened release"
                continue
            self.observed["attempts"] = attempt
            self.mitigation_oracle.capture_handover()
            logger.info("Reopened-release injection: %s", json.dumps(self.observed, default=str))
            return
        raise RuntimeError(f"A task-worker rollout during the release did not reopen it: {self.observed}")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        """Reference mitigation: the release is on main, so mark its branch MERGED again."""
        if self.mitigation_oracle.evaluate().get("success"):
            return
        script = 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}" "$0"'
        query = f"MATCH (b:Branch {{name: '{RELEASE}'}}) WHERE b.status = 'OPEN' SET b.status = 'MERGED'"
        self.app.command("exec", "deploy/database", "--", "sh", "-c", script, query, timeout=120)
        # Every worker picks the status up from the database on its next branch refresh.
        self.app.run_client("branches")
