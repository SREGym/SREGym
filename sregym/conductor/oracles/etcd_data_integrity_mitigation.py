"""Mitigation oracle for etcd data integrity bugs.

Verifies that etcd returns correct data after the agent fixes a bug.
Used for non-crash bugs where the symptom is silent data corruption
or missing events (e.g., #18089 watch dropping DELETE events,
#14733 revision inconsistency after defrag kill).

The oracle runs a verification script via an alpine client pod:
  1. Writes a known set of key-value pairs
  2. Performs the operation that triggers the bug (compact, defrag, etc.)
  3. Reads back data and compares against expected values
  4. Reports pass/fail based on data consistency
"""

import logging
import subprocess
import time

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)

_SETTLE_TIMEOUT_S = 60
_POLL_INTERVAL_S = 5


class EtcdDataIntegrityOracle(Oracle):
    importance = 1.0

    def __init__(self, problem, cluster_name: str, verification_script: str):
        """
        Args:
            cluster_name: etcd service name for endpoint construction.
            verification_script: shell script that exits 0 if data is
                consistent, non-zero if the bug is still present.
                The script is run in an alpine pod with etcdctl available
                and ETCDCTL_ENDPOINTS set to the cluster service.
        """
        super().__init__(problem)
        self.cluster_name = cluster_name
        self.verification_script = verification_script

    def evaluate(self) -> dict:
        namespace = self.problem.namespace
        logger.info(
            f"[EtcdDataIntegrity] Running verification in '{namespace}' "
            f"against cluster '{self.cluster_name}'"
        )

        deadline = time.time() + _SETTLE_TIMEOUT_S
        last_reason = ""
        while time.time() < deadline:
            passed, reason = self._run_check(namespace)
            last_reason = reason
            if passed:
                logger.info(f"[EtcdDataIntegrity] PASS — {reason}")
                return {"success": True, "reason": reason}
            time.sleep(_POLL_INTERVAL_S)

        logger.info(f"[EtcdDataIntegrity] FAIL — {last_reason}")
        return {"success": False, "reason": last_reason}

    def _run_check(self, namespace: str) -> tuple[bool, str]:
        svc = f"{self.cluster_name}.{namespace}.svc.cluster.local"
        endpoint = f"http://{svc}:2379"
        pod = "etcd-integrity-check"

        try:
            subprocess.run(
                f"kubectl delete pod {pod} -n {namespace} --ignore-not-found",
                shell=True, capture_output=True,
            )
            subprocess.run(
                f"kubectl run {pod} --image=alpine:3.20 "
                f"--restart=Never -n {namespace} -- sleep 300",
                shell=True, check=True, capture_output=True,
            )
            subprocess.run(
                f"kubectl wait pod/{pod} -n {namespace} "
                f"--for=condition=Ready --timeout=60s",
                shell=True, check=True, capture_output=True,
            )
            subprocess.run(
                f"kubectl exec {pod} -n {namespace} -- "
                "sh -c 'wget -qO /tmp/etcd.tgz "
                "https://github.com/etcd-io/etcd/releases/download/v3.5.17/"
                "etcd-v3.5.17-linux-amd64.tar.gz && "
                "tar xzf /tmp/etcd.tgz -C /tmp && "
                "cp /tmp/etcd-v3.5.17-linux-amd64/etcdctl /usr/local/bin/ && "
                "rm -rf /tmp/etcd*'",
                shell=True, check=True, capture_output=True, timeout=120,
            )
            result = subprocess.run(
                f"kubectl exec -i {pod} -n {namespace} -- "
                f"sh -c 'export ETCDCTL_ENDPOINTS={endpoint}; sh'",
                shell=True, input=self.verification_script,
                capture_output=True, text=True, timeout=120,
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode == 0:
                return True, f"data integrity verified: {output[:200]}"
            return False, f"data integrity failed (rc={result.returncode}): {output[:300]}"
        except subprocess.TimeoutExpired:
            return False, "verification script timed out"
        except Exception as e:
            return False, f"verification error: {e}"
        finally:
            subprocess.run(
                f"kubectl delete pod {pod} -n {namespace} "
                f"--ignore-not-found --wait=false",
                shell=True, capture_output=True,
            )
