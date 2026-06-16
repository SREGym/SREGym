"""Confirmed reproducible: https://github.com/etcd-io/etcd/issues/13937

Title: etcd panics on restart when auth is enabled with low snapshot-count
Buggy: v3.5.3 only (regression from PR #13908). Fixed: v3.5.4 (PR #13942).

Reproduction (verified 2026-04-28):
  1. Deploy 3-node etcd v3.5.4 (stock/fixed), swap to v3.5.3 (buggy)
  2. With ETCD_SNAPSHOT_COUNT=3, enable auth, send 20 unauthenticated PUTs
  3. Force-kill all pods
  4. All pods panic on restart: "failed to recover v3 backend from snapshot"
  5. Permanent CrashLoopBackOff — cluster is unrecoverable without upgrade

Root cause: In v3.5.3, auth-rejected requests (permission denied, empty username)
still create raft log entries but skip LockInsideApply(), so consistent_index is
never advanced. With snapshot-count=3, 3 such entries trigger a raft snapshot, but
the .snap.db file is never written to disk (because nothing was actually applied).
On restart, etcd looks for the snapshot file and panics at server.go:515.
"""

import json as _json
import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoEtcd13937(GenericCustomBuildProblem):
    db_name = "etcd"
    db_version = "3.5.5"
    source_git_ref = "v3.5.3"
    build_image = "golang:1.19"
    root_cause_description = (
        "etcd v3.5.3 panics on restart when auth is enabled and a low "
        "snapshot-count is configured. Auth-rejected requests (empty username) "
        "create raft log entries but do not advance the consistent_index via "
        "LockInsideApply(). With ETCD_SNAPSHOT_COUNT=3, three such entries "
        "trigger a raft snapshot, but the .snap.db file is never written "
        "because no state was actually applied. On restart, etcd cannot find "
        "the snapshot file and panics with 'failed to recover v3 backend "
        "from snapshot'. The cluster enters permanent CrashLoopBackOff."
    )
    extra_helm_args = (
        "--set replicaCount=3 "
        "--set-string 'extraEnvVars[0].name=ETCD_SNAPSHOT_COUNT' "
        "--set-string 'extraEnvVars[0].value=3'"
    )
    reproducer = (
        "#!/bin/sh\n"
        "for i in 1 2 3 4 5 6; do\n"
        "  etcdctl endpoint health > /dev/null 2>&1\n"
        "done\n"
        "etcdctl endpoint health 2>&1\n"
    )
    continuous_reproducer = True

    @mark_fault_injected
    def inject_fault(self):
        """Wipe data, swap to buggy image, trigger snapshot bug, kill all."""
        ns = self.app.namespace
        sts_name = self.app.cluster_name

        logger.info("[etcd#13937] Scaling down for clean PVC wipe")
        subprocess.run(
            f"kubectl scale statefulset {sts_name} -n {ns} --replicas=0",
            shell=True, capture_output=True, text=True,
        )
        deadline = time.time() + 120
        while time.time() < deadline:
            out = subprocess.run(
                f"kubectl get pods -n {ns} -l app.kubernetes.io/instance={sts_name} --no-headers",
                shell=True, capture_output=True, text=True,
            )
            if not out.stdout.strip():
                break
            time.sleep(5)
        subprocess.run(
            f"kubectl delete pvc -n {ns} -l app.kubernetes.io/instance={sts_name}",
            shell=True, capture_output=True, text=True,
        )
        time.sleep(5)

        logger.info(f"[etcd#13937] Swapping to buggy image: {self._custom_image}")
        self.app.inject_buggy_image(self._custom_image)

        logger.info("[etcd#13937] Running reproducer: 6x endpoint health to trigger snapshot")
        self.app.run_reproducer(self.reproducer)

        logger.info("[etcd#13937] Force-deleting all etcd pods (equivalent to kill -9)")
        subprocess.run(
            f"kubectl delete pod -n {ns} "
            f"-l app.kubernetes.io/instance={sts_name} "
            f"--force --grace-period=0",
            shell=True, check=True, capture_output=True, text=True,
        )

        logger.info("[etcd#13937] Waiting for CrashLoopBackOff")
        self._wait_for_any_crash_loop(timeout=300)
        logger.info("[etcd#13937] CrashLoopBackOff confirmed — fault injected")

        if self.continuous_reproducer and self.reproducer:
            self.app.deploy_continuous_reproducer(self.reproducer, self.expected_output)

    @mark_fault_injected
    def recover_fault(self):
        """Wipe corrupted PVCs and restore stock (fixed) image."""
        ns = self.app.namespace
        sts_name = self.app.cluster_name
        logger.info("[etcd#13937] Recovering from CrashLoopBackOff: deleting pods + PVCs")
        subprocess.run(
            f"kubectl delete pod -n {ns} -l app.kubernetes.io/instance={sts_name} "
            f"--force --grace-period=0",
            shell=True, capture_output=True, text=True,
        )
        time.sleep(5)
        subprocess.run(
            f"kubectl delete pvc -n {ns} -l app.kubernetes.io/instance={sts_name} "
            f"--wait=false",
            shell=True, capture_output=True, text=True,
        )
        time.sleep(5)
        logger.info("[etcd#13937] Restoring stock (fixed) image")
        self.app.restore_stock_image(custom_image=self._custom_image)

    def _wait_for_any_crash_loop(self, timeout: int = 300):
        """Wait until any pod in the namespace enters CrashLoopBackOff."""
        ns = self.app.namespace
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = subprocess.run(
                f"kubectl get pods -n {ns} -o json",
                shell=True, capture_output=True, text=True,
            )
            try:
                data = _json.loads(out.stdout)
                for pod in data.get("items", []):
                    for cs in pod.get("status", {}).get("containerStatuses", []):
                        reason = (
                            cs.get("state", {}).get("waiting", {}).get("reason", "")
                        )
                        exit_code = (
                            cs.get("state", {}).get("terminated", {}).get("exitCode")
                        )
                        restarts = cs.get("restartCount", 0)
                        if reason in ("CrashLoopBackOff", "Error") or (
                            exit_code is not None and exit_code != 0 and restarts >= 2
                        ):
                            pod_name = pod["metadata"]["name"]
                            logger.info(
                                f"Crash confirmed on '{pod_name}': "
                                f"reason={reason or 'exit'} restarts={restarts}"
                            )
                            return
            except Exception:
                pass
            time.sleep(10)
        raise RuntimeError(
            f"Timeout ({timeout}s) waiting for CrashLoopBackOff in '{ns}'"
        )
