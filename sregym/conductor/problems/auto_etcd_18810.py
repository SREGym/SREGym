"""Confirmed reproducible: https://github.com/etcd-io/etcd/issues/18810

Title: etcd crashes when running out of space during defrag
Buggy: all v3.5.x through v3.5.16. Fixed: v3.5.17 (PR #18842).

Reproduction (verified 2026-04-27):
  1. Deploy etcd v3.5.16 with tmpfs storage (100Mi)
  2. Write ~20MB of data (200 keys * 100KB)
  3. Run `etcdctl defrag` → fails with ENOSPC for db.tmp
  4. Any subsequent write → panic: nil pointer dereference at bbolt.(*Tx).Bucket()

Root cause: defrag() nils batchTx.tx before copying to temp DB. If the copy
fails (ENOSPC), the nil tx is never restored. Next operation dereferences it.
Fix (v3.5.17): transactions are restored on defrag failure path.
"""

import json
import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoEtcd18810(GenericCustomBuildProblem):
    db_name = "etcd"
    db_version = "3.5.17"
    source_git_ref = "v3.5.16"
    root_cause_description = (
        "etcd crashes with nil pointer dereference after defrag fails due to "
        "insufficient disk space. During defrag, batchTx.tx and readTx.tx are "
        "set to nil before copying data to a temp file. If the copy fails "
        "(ENOSPC on db.tmp), the transactions stay nil. The next backend "
        "operation dereferences the nil pointer and panics. "
        "Bug in server/storage/backend/backend.go defrag()."
    )
    extra_helm_args = "--set persistence.enabled=false"
    reproducer = (
        "#!/bin/sh\n"
        "VALUE=$(dd if=/dev/urandom bs=1024 count=100 2>/dev/null | base64 | head -c 100000)\n"
        "for i in $(seq 1 200); do\n"
        "  etcdctl put \"fillkey$i\" \"$VALUE\" > /dev/null 2>&1 || break\n"
        "done\n"
        "etcdctl defrag > /dev/null 2>&1\n"
        "sleep 2\n"
        "etcdctl put probe_after_defrag ok 2>&1\n"
    )
    continuous_reproducer = True

    def post_deploy(self):
        """Patch the data volume to use tmpfs (Memory) with 100Mi limit."""
        sts_name = self.app.cluster_name
        ns = self.app.namespace
        patch = json.dumps({
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [{
                            "name": "data",
                            "emptyDir": {"medium": "Memory", "sizeLimit": "100Mi"},
                        }]
                    }
                }
            }
        })
        logger.info(f"[etcd#18810] Patching StatefulSet '{sts_name}' data volume → tmpfs 100Mi")
        subprocess.run(
            f"kubectl patch statefulset {sts_name} -n {ns} --type=strategic -p '{patch}'",
            shell=True, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            f"kubectl delete pod -n {ns} -l app.kubernetes.io/instance={sts_name} "
            f"--force --grace-period=0",
            shell=True, capture_output=True, text=True,
        )
        logger.info("[etcd#18810] Waiting for pod restart with tmpfs volume")
        time.sleep(5)
        self.app._wait_for_cluster_ready(timeout=300)
        logger.info("[etcd#18810] Pod restarted with tmpfs volume")

