"""A Redis restart during a branch merge wedges one task worker, and then every merge in the cluster.

Upstream bug (Infrahub 1.11.9, unfixed and unpublished at the time of writing;
found and reproduced by Specula as MC-1): a merge holds
``lock.registry.global_graph_lock()``, an ``InfrahubMultiLock`` of the
per-process ``local.schema`` asyncio lock plus the Redis ``global.graph`` and
``global.schema`` locks (no TTL). When Redis restarts empty while the merge runs,
the merge fails and is rolled back, and ``InfrahubMultiLock.release``
(``backend/infrahub/lock.py``) releases the Redis locks first: the first
``LockNotOwnedError`` (or ``ConnectionError``) aborts the loop, so ``local.schema``
is never released. The task worker keeps heartbeating. Its schema registry can no
longer refresh (new branches are unknown to it, branch creation on it hangs), and
the next merge it picks up for a branch it knows takes the cluster-wide
``lock.merge.all_branches`` lock and then waits forever for ``local.schema``.
Every later merge in the cluster queues behind that lock. Infrahub's lock reaper
only removes locks of inactive workers, and ``infrahub recover merge`` finds no
failed merge.

The natural event is the Redis cache container being OOM-killed (it has a
memory limit and no maxmemory) and restarted empty while a large change branch
is being merged. The conductor fills the cache past its limit from inside the
cache container at the right moment; the agent sees an ordinary OOMKilled
restart. A routine inventory-sync rollout some minutes earlier gives the cluster
other recent pod events, and the incident ages 25 minutes (inventory-sync keeps
working) before handover. A failed attempt redeploys the namespace from scratch,
so no retry branch or extra restart is visible. The agent gets the plain
application description only; the symptom shows in inventory-sync's log (merges
that never complete, branch creations that time out). Specula's MC-3 (a queued second merge running concurrently and being
erased by the first merge's rollback) needs a merge or main write inside the few
seconds between the restart and the rollback; the injection starts the large merge
just after a scheduled change merge and the inventory writer holds main writes
while a branch is MERGING, so MC-3 does not fire. Only the in-flight large merge
is rolled back (its branch returns to OPEN), which is the merge's own failure.
"""

import json
import logging
import select
import subprocess
import time

from sregym.conductor.oracles.apps.infrahub.branch_merge import InfrahubBranchMergeOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.infrahub.application import InfrahubApplication
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.sregym.problems.infrahub")

CHANGE_INTERVAL = 90
REVIEW_SLOTS = 4
LARGE_BRANCH_DEVICES = 2500
#: Seconds after the merge starts its graph phase (diff lock taken) before the cache is OOM-killed.
RESTART_DELAY = 1.0
#: Latest restart, in seconds after the quiet moment, that leaves the failed merge time to roll
#: back before the next scheduled change merge.
LATEST_RESTART = CHANGE_INTERVAL - 50
#: The wedge counts as established once the merge lock has been held this long by a live worker.
WEDGE_HOLD_SECONDS = 90
VERIFY_SECONDS = 720
MAX_ATTEMPTS = 3
#: The routine inventory-sync rollout happens this long before the cache is OOM-killed (roughly).
ROLLOUT_LEAD_S = 8 * 60
#: The incident ages this long before the agent is handed the cluster.
PROPAGATION_S = 25 * 60
#: Two strings this large exceed the cache container's 512Mi limit (Redis runs without maxmemory).
FILL_BYTES = 400_000_000

ROOT_CAUSE = (
    "The Redis cache container was OOM-killed and restarted empty (no persistence) while a task-worker was "
    "merging the large dc-expansion branch and held global_graph_lock: the worker process's in-process "
    "local.schema asyncio lock plus the Redis global.graph and global.schema locks (no TTL). The merge failed and "
    "was rolled back (dc-expansion went back to OPEN; nothing of it reached main). Upstream Infrahub 1.11.9 "
    "defect: InfrahubMultiLock.release (backend/infrahub/lock.py) releases the Redis locks first, and the "
    "LockNotOwnedError raised for the lock Redis lost aborts the release loop, so that worker process never "
    "released its in-memory local.schema lock. The process stayed alive and kept heartbeating, but wedged: its "
    "schema registry can no longer refresh (branch creations it handles hang), and the next merge it picked up "
    "took the cluster-wide lock.merge.all_branches Redis lock under its own worker identity and then waited "
    "forever for local.schema, so every later merge in the cluster queues behind that lock and never completes. "
    "Infrahub's deadlock reaper only removes locks of inactive workers and infrahub recover merge finds no failed "
    "merge, because the holder is a live worker. Correct repair: restart only the wedged task-worker pod (the "
    "process that logged the LockNotOwnedError and whose identity holds lock.merge.all_branches; the leaked "
    "asyncio lock cannot be released from outside the process), make sure the merge lock it held is gone once it "
    "is dead (delete it or let the reaper remove it), leave the healthy task-worker running, and re-merge as "
    "needed. "
    "Grading requirements: the mechanism is required for fault characterization (D2): the in-process local.schema "
    "lock leaked in one live task-worker process because the multi-lock release aborted on the lost Redis lock. "
    "A diagnosis that blames only an orphaned or stale Redis lock (for example a lock left by a dead or crashed "
    "worker, or lock keys lost or corrupted in the Redis restart) without the leaked in-process lock in a live "
    "worker states the wrong mechanism; so does one that blames Redis memory, the write protection (merge:protected) "
    "key, a branch stuck in MERGING that needs infrahub recover merge, or the infrahub-server. In any of these "
    "cases answer No to D2-Q1, D2-Q2 and D2-Q3."
)


class InfrahubRedisRestartWedgedMergeLock(Problem):
    def __init__(self):
        app = InfrahubApplication()
        super().__init__(app)
        app.create_workload(CHANGE_INTERVAL=CHANGE_INTERVAL, REVIEW_SLOTS=REVIEW_SLOTS)
        self.kubectl = app.kubectl
        self.faulty_service = "task-worker"
        self.wedged_worker = None
        self.observed = {}
        self.healthy_worker = None
        self.propagation_duration_s = PROPAGATION_S
        self.root_cause = self.build_structured_root_cause(
            component="task-worker", namespace=self.namespace, description=ROOT_CAUSE
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = InfrahubBranchMergeOracle(self)
        app.describe()

    # ------------------------------------------------------------------ helpers
    def _branches(self):
        return self.app.run_client("branches")

    def _wait_backlog(self, timeout=600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            backlog = [b for b, s in self._branches().items() if b.startswith("change-") and s == "OPEN"]
            if len(backlog) >= REVIEW_SLOTS - 1:
                return backlog
            time.sleep(15)
        raise RuntimeError("inventory-sync did not build up a change backlog")

    def _quiet_moment(self):
        """Sleep until just after a scheduled change merge, with no merge running; return that moment."""
        while True:
            now = time.time()
            slot = int(now) // CHANGE_INTERVAL * CHANGE_INTERVAL
            start = slot + 20 if now < slot + 20 else slot + CHANGE_INTERVAL + 20
            time.sleep(max(0.0, start - time.time()))
            busy = self.app.redis("get", "lock.merge.all_branches").strip() or any(
                s == "MERGING" for s in self._branches().values()
            )
            if not busy and time.time() - start < 8:
                return start

    def _watch_graph_merge(self, timeout=300):
        """Block until a task worker logs that the merge holds its diff lock (graph phase starting).

        From here the merge holds global.graph and global.schema and spends several seconds
        writing the graph before it touches Redis again. Returns the pod name or None.
        """
        proc = subprocess.Popen(
            [
                "kubectl",
                "-n",
                self.namespace,
                "logs",
                "-f",
                "-l",
                "app=task-worker",
                "--prefix",
                "--since=5s",
                "--max-log-requests=8",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ready, _, _ = select.select([proc.stdout], [], [], 1)
                if ready:
                    line = proc.stdout.readline()
                    if not line:
                        return None
                    if "Diff lock acquired for merge" in line:
                        return line.split("]", 1)[0].lstrip("[").split("/")[1]
            return None
        finally:
            proc.kill()
            proc.wait()

    def _worker_pod(self, identity, branch):
        """The task-worker pod whose process logged under ``identity`` (or ran the merge)."""
        app = self.app
        fallback = None
        for pod in app.pods("task-worker"):
            name = pod["metadata"]["name"]
            logs = app.logs(name)
            record = {
                "pod": name,
                "uid": pod["metadata"]["uid"],
                "restarts": sum(c.get("restartCount", 0) for c in pod["status"].get("containerStatuses", [])),
                "identity": identity,
            }
            if identity in logs:
                return record
            if f"Merge branch {branch} into main" in logs:
                fallback = record
        return fallback

    def _verify_wedge(self, identity, large_branch):
        """Wait until a merge lock taken by the wedged worker is held while it keeps heartbeating."""
        app = self.app
        held_since, token_seen = None, None
        deadline = time.monotonic() + VERIFY_SECONDS
        while time.monotonic() < deadline:
            token = app.redis("get", "lock.merge.all_branches").strip()
            alive = app.redis("exists", f"workers:active:git_agent:worker:{identity}").strip() == "1"
            if token.endswith(identity) and alive:
                if token != token_seen:
                    token_seen, held_since = token, time.monotonic()
                elif time.monotonic() - held_since >= WEDGE_HOLD_SECONDS:
                    return {"merge_lock": token, "held_seconds": round(time.monotonic() - held_since)}
            else:
                token_seen = held_since = None
            time.sleep(10)
        return None

    def _wait_merge_settled(self, branch, timeout=300):
        status = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self._branches().get(branch)
            except Exception:
                status = None
            if status in ("OPEN", "MERGED", "MERGE_FAILED"):
                return status
            time.sleep(5)
        return status

    def _check_no_merged_data_lost(self, record):
        """Every change branch reported MERGED must have its device on main (MC-3 would erase it)."""
        branches = self._branches()
        merged = [f"dev-{b}" for b, s in branches.items() if b.startswith("change-") and s == "MERGED"]
        missing = self.app.run_client("missing_devices", names=merged)["missing"]
        record["merged_changes_checked"] = len(merged)
        if missing:
            raise RuntimeError(f"Merged change data missing from main after the Redis restart: {missing}")

    def _cache_restarts(self):
        pods = self.app.pods("cache")
        return sum(c.get("restartCount", 0) for p in pods for c in p["status"].get("containerStatuses", []))

    def _oom_kill_cache(self):
        """Grow the Redis cache past its container memory limit; the kernel OOM-kills it and kubelet restarts it."""
        app = self.app
        before = self._cache_restarts()
        for key in ("a", "b"):
            try:
                app.command(
                    "exec",
                    "deploy/cache",
                    "--",
                    "redis-cli",
                    "SETRANGE",
                    f"buffer:{key}",
                    str(FILL_BYTES),
                    "0",
                    timeout=60,
                )
            except Exception:  # the exec stream dies with the container
                break
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            pods = app.pods("cache")
            statuses = [c for p in pods for c in p["status"].get("containerStatuses", [])]
            if self._cache_restarts() > before and statuses and all(c.get("ready") for c in statuses):
                last = statuses[0].get("lastState", {}).get("terminated", {})
                return {"reason": last.get("reason"), "exit_code": last.get("exitCode")}
            time.sleep(1)
        raise RuntimeError("The cache container was not OOM-killed and restarted")

    def _healthy_worker(self, wedged):
        for pod in self.app.pods("task-worker"):
            if pod["metadata"]["uid"] != wedged["uid"]:
                return {
                    "pod": pod["metadata"]["name"],
                    "uid": pod["metadata"]["uid"],
                    "restarts": sum(c.get("restartCount", 0) for c in pod["status"].get("containerStatuses", [])),
                }
        return None

    def _reap_dead_locks(self):
        """Delete lock keys whose holder is no longer an active worker (what clean-up-deadlocks does after 15 min)."""
        app = self.app
        for key in app.redis("--scan", "--pattern", "lock.*").split():
            token = app.redis("get", key).strip()
            holder = token.partition("::")[2]
            if (
                holder
                and app.redis("exists", f"workers:active:git_agent:worker:{holder}").strip() != "1"
                and (app.redis("exists", f"workers:active:api_server:worker:{holder}").strip() != "1")
            ):
                app.redis("del", key)

    # ------------------------------------------------------------------ lifecycle
    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                # A clean slate: retry branches, extra restarts or a recovered merge would be visible.
                app.redeploy()
                self.mitigation_oracle.capture_baseline()
            branch = "dc-expansion"
            record = {"branch": branch}
            self.observed[f"attempt_{attempt}"] = record
            rolled_out = time.monotonic()
            # Routine change some minutes before the event: inventory-sync is rolled out again.
            app.command("rollout", "restart", "deployment/inventory-sync")
            app.rollout("inventory-sync", timeout=600)
            record["backlog"] = self._wait_backlog()
            app.run_client("branch_change", branch=branch, devices=LARGE_BRANCH_DEVICES, merge=False, timeout=900)
            time.sleep(max(0.0, ROLLOUT_LEAD_S - (time.monotonic() - rolled_out) - CHANGE_INTERVAL))
            quiet = self._quiet_moment()
            app.run_client("merge", branch=branch)
            merging_pod = self._watch_graph_merge()
            token = app.redis("get", "lock.global.graph").strip()
            if not merging_pod or "::" not in token:
                record["outcome"] = "merge never reached its graph phase"
                continue
            identity = token.partition("::")[2]
            record["merging_pod"] = merging_pod
            time.sleep(RESTART_DELAY)
            if time.time() > quiet + LATEST_RESTART:
                # Too close to the next scheduled change merge: a merge queued during the rollback
                # would run concurrently with it (Specula MC-3).
                record["outcome"] = "graph lock taken too late in the change interval"
                continue
            # The natural event: the cache container is OOM-killed and comes back empty.
            record["redis_restarted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            record["cache_termination"] = self._oom_kill_cache()
            status = self._wait_merge_settled(branch)
            record["merge_outcome"] = status
            record["settled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if status != "OPEN":
                record["outcome"] = f"merge did not roll back to OPEN ({status})"
                continue
            self.wedged_worker = self._worker_pod(identity, branch)
            record["wedged_worker"] = self.wedged_worker
            wedge = self._verify_wedge(identity, branch)
            if wedge:
                record.update(wedge)
                self.healthy_worker = self._healthy_worker(self.wedged_worker)
                record["healthy_worker"] = self.healthy_worker
                self.observed["attempts"] = attempt
                self._check_no_merged_data_lost(record)
                logger.info("Merge-lock injection: %s", json.dumps(self.observed))
                return
            record["outcome"] = "no merge got stuck behind the wedged worker"
            self.wedged_worker = None
        raise RuntimeError(f"Redis restart during a merge did not wedge merges: {json.dumps(self.observed)}")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        """Reference mitigation: restart only the wedged worker, drop its orphaned locks, recover failed merges."""
        verdict = self.mitigation_oracle.evaluate()
        if verdict.get("success"):
            return
        logger.info("Merge-lock recovery starts from: %s", verdict)
        app = self.app
        wedged = self.wedged_worker
        for pod in app.pods("task-worker"):
            restarts = sum(c.get("restartCount", 0) for c in pod["status"].get("containerStatuses", []))
            if wedged and pod["metadata"]["uid"] == wedged["uid"] and restarts <= wedged["restarts"]:
                app.command("delete", "pod", pod["metadata"]["name"], "--wait=true", timeout=180)
        app.rollout("task-worker", timeout=600)
        app.wait_workers_registered()
        time.sleep(20)  # let the deleted processes' worker heartbeat keys expire (15 s TTL)
        self._reap_dead_locks()
        # Only a merge Infrahub flagged as failed needs recovery; never run it against a live merge.
        if any(s == "MERGE_FAILED" for s in self._branches().values()):
            app.command("exec", "deploy/infrahub-server", "--", "infrahub", "recover", "merge", "--yes", timeout=600)
