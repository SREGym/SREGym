"""Branch merges, branch creation and schema loads must complete again, with merged data intact.

White-box: the task-worker process whose in-memory schema lock leaked must be
gone (the leak cannot be released from outside the process), the healthy
task-worker must still be the same, unrestarted process, and no merge lock may
be held by a live worker while nothing merges, or linger after its holder died.
Only one process is wedged, so restarting every worker is a blind fix: it also
kills the healthy worker's in-flight flows (merges queued behind the lock are
not resubmitted), and the incident's lock is older than the reaper's 15-minute
threshold by handover, so the reaper would clear it on its own and nothing
would show the responder understood the lock. Black-box: several fresh
change branches are created and merged in a row (each lands on a random task
worker), a schema load completes, the device of every change branch reported
MERGED and of every probe is on main, and no branch is left MERGING or MERGE_FAILED.
"""

import time
from datetime import datetime

from sregym.conductor.oracles.apps.infrahub.platform import InfrahubPlatformOracle
from sregym.conductor.oracles.failure import FailureClass


def _epoch(timestamp):
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


class InfrahubBranchMergeOracle(InfrahubPlatformOracle):
    FAILURE_CLASSES = {
        "wedged_worker_still_running": FailureClass.AGENT_ERROR,
        "healthy_worker_restarted": FailureClass.AGENT_ERROR,
        "merge_lock_still_held": FailureClass.AGENT_ERROR,
        "merge_did_not_complete": FailureClass.AGENT_ERROR,
        "merged_data_missing": FailureClass.AGENT_ERROR,
        "branch_left_in_merge_state": FailureClass.AGENT_ERROR,
        "schema_load_did_not_complete": FailureClass.AGENT_ERROR,
    }
    evaluation_timeout_seconds = 1800
    probe_merges = 4
    merge_timeout_seconds = 240
    #: A dead holder's merge lock may linger this long (clean-up-deadlocks runs every minute and
    #: the incident's lock is already past its 15-minute age threshold).
    dead_holder_grace_seconds = 180

    def wedged_process_alive(self):
        wedged = getattr(self.problem, "wedged_worker", None)
        if not wedged:
            return None
        for pod in self.app.pods("task-worker"):
            if pod["metadata"]["uid"] != wedged["uid"]:
                continue
            restarts = sum(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", []))
            if restarts <= wedged["restarts"]:
                return self.fail("wedged_worker_still_running", pod=wedged["pod"])
        return None

    def healthy_worker_restarted(self):
        healthy = getattr(self.problem, "healthy_worker", None)
        if not healthy:
            return None
        for pod in self.app.pods("task-worker"):
            if pod["metadata"]["uid"] == healthy["uid"]:
                restarts = sum(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", []))
                if restarts <= healthy["restarts"] and not pod["metadata"].get("deletionTimestamp"):
                    return None
        return self.fail("healthy_worker_restarted", pod=healthy["pod"])

    def wait_merge_lock_released(self):
        """Wait out a merge in progress, or the last moments of a dead holder's merge lock.

        A live holder releases the lock when its merge ends. A dead holder's lock is
        deleted by clean-up-deadlocks within a minute or two (or by the responder).
        A lock that outlives both is a wedge.
        """
        app = self.app
        live_deadline = time.monotonic() + self.merge_timeout_seconds
        dead_deadline = time.monotonic() + self.dead_holder_grace_seconds
        while True:
            token = app.redis("get", "lock.merge.all_branches").strip()
            if not token:
                return None
            taken, _, holder = token.partition("::")
            alive = app.redis("exists", f"workers:active:git_agent:worker:{holder}").strip() == "1"
            if alive and time.monotonic() >= live_deadline:
                return self.fail("merge_lock_still_held", holder=holder, holder_active=True, since=taken)
            if not alive and time.monotonic() >= dead_deadline and time.time() - _epoch(taken) > 60:
                return self.fail("merge_lock_still_held", holder=holder, holder_active=False, since=taken)
            time.sleep(10)

    def evaluate(self):
        if not self.baseline:
            return self.fail("baseline_not_captured")
        app = self.app
        try:
            if failed := self.platform_intact():
                return failed
            if failed := self.wedged_process_alive():
                return failed
            if failed := self.healthy_worker_restarted():
                return failed
            if failed := self.wait_merge_lock_released():
                return failed
            merged = []
            for _ in range(self.probe_merges):
                try:
                    result = app.run_client("branch_change", devices=1, timeout=self.merge_timeout_seconds)
                except Exception as exc:
                    return self.fail("merge_did_not_complete", error=str(exc)[-300:], completed=merged)
                if not result["merged"]:
                    return self.fail("merge_did_not_complete", result=result, completed=merged)
                merged.append(result["branch"])
            touched = app.run_client("touch_schema", description=f"Network device (checked {int(time.time())})")
            if touched["status"] != 200:
                return self.fail("schema_load_did_not_complete", result=touched)
            # Every change branch Infrahub reports MERGED (before, during or after the incident)
            # must have its device on main, as must the probes' devices.
            branches = app.run_client("branches")
            expected = [f"dev-{b}" for b, s in branches.items() if b.startswith("change-") and s == "MERGED"]
            expected += [f"dev-{b}-0000" for b in merged]
            missing = app.run_client("missing_devices", names=expected, exec_timeout=300)["missing"]
            if missing:
                return self.fail("merged_data_missing", missing=missing[:5])
            deadline = time.monotonic() + self.merge_timeout_seconds
            while True:
                # A MERGING branch may be a live merge (inventory-sync ships changes); wait it out.
                stuck = {b: s for b, s in app.run_client("branches").items() if s in ("MERGING", "MERGE_FAILED")}
                if not stuck:
                    break
                if "MERGE_FAILED" in stuck.values() or time.monotonic() >= deadline:
                    return self.fail("branch_left_in_merge_state", branches=stuck)
                time.sleep(10)
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            "success": True,
            "probe_merges_completed": merged,
            "pre_incident_devices_verified": len(self.baseline["devices"]),
            "merged_changes_verified": len(expected),
        }
