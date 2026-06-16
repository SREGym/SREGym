"""Confirmed reproducible: https://github.com/etcd-io/etcd/issues/14382

Title: etcd panics on restart after alarm-only applies trigger snapshot
Buggy: v3.5.3, v3.5.4. Fixed: v3.5.5 (PR #14429).

Reproduction (verified 2026-04-27):
  1. Deploy etcd v3.5.5 (stock/fixed), swap to v3.5.4 (buggy)
  2. With ETCD_SNAPSHOT_COUNT=5, run `etcdctl endpoint health` 6 times
  3. Force-kill the etcd pod (kubectl delete --force)
  4. etcd restarts → panic: "failed to recover v3 backend from snapshot"
  5. Permanent CrashLoopBackOff — data is unrecoverable without upgrade

Root cause: `endpoint health` calls `alarm list` which goes through raft apply
but does NOT advance consistent_index in the v3 backend DB. After 5 alarm-only
applies (matching snapshot-count=5), a raft snapshot is triggered at index 6.
On hard kill + restart, etcd looks for 0000000000000006.snap.db which was never
created. Panic at server.go:515.
"""

import json as _json
import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoEtcd14382(GenericCustomBuildProblem):
    db_name = "etcd"
    db_version = "3.5.5"
    source_git_ref = "v3.5.4"
    build_image = "golang:1.19"
    root_cause_description = (
        "etcd panics on restart after alarm-only raft applies trigger a "
        "snapshot. endpoint health internally calls alarm list, which goes "
        "through raft apply but does not advance the consistent_index in the "
        "v3 backend. With ETCD_SNAPSHOT_COUNT=5, running endpoint health 6 "
        "times triggers a snapshot at index 6. After kill -9 and restart, "
        "etcd cannot find the expected snap.db file and panics with 'failed "
        "to recover v3 backend from snapshot'. The data directory is "
        "permanently corrupted."
    )
    extra_helm_args = (
        "--set-string 'extraEnvVars[0].name=ETCD_SNAPSHOT_COUNT' "
        "--set-string 'extraEnvVars[0].value=5'"
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
        """Swap to buggy image, trigger the bug, then force-kill."""
        ns = self.app.namespace
        sts_name = self.app.cluster_name

        logger.info(f"[etcd#14382] Swapping to buggy image: {self._custom_image}")
        self.app.inject_buggy_image(self._custom_image)

        logger.info("[etcd#14382] Running reproducer: 6x endpoint health")
        self.app.run_reproducer(self.reproducer)

        logger.info("[etcd#14382] Force-deleting etcd pod (equivalent to kill -9)")
        subprocess.run(
            f"kubectl delete pod -n {ns} "
            f"-l app.kubernetes.io/instance={sts_name} "
            f"--force --grace-period=0",
            shell=True, check=True, capture_output=True, text=True,
        )

        logger.info("[etcd#14382] Waiting for CrashLoopBackOff")
        self._wait_for_any_crash_loop(timeout=300)
        logger.info("[etcd#14382] CrashLoopBackOff confirmed — fault injected")

        if self.continuous_reproducer and self.reproducer:
            self.app.deploy_continuous_reproducer(self.reproducer, self.expected_output)

    @mark_fault_injected
    def recover_fault(self):
        """Wipe corrupted PVCs and restore stock (fixed) image."""
        ns = self.app.namespace
        sts_name = self.app.cluster_name
        self._recover_crashloop(ns, sts_name)

    def _recover_crashloop(self, ns: str, sts_name: str):
        """Delete PVCs + pods, swap to stock image, wait for healthy cluster."""
        logger.info("[etcd] Recovering from CrashLoopBackOff: deleting pods + PVCs")
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
        logger.info("[etcd] Restoring stock (fixed) image")
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
