"""Confirmed reproducible: https://github.com/etcd-io/etcd/issues/14110

Title: etcd panics on serializable readonly txn after SIGSTOP/SIGCONT
Buggy: v3.5.0 through v3.5.4. Fixed: v3.5.5 (PR #14178).

Reproduction (verified 2026-04-28):
  1. Deploy etcd v3.5.5 (stock/fixed), swap to v3.5.4 (buggy)
  2. Add sidecar that populates keys and periodically triggers
     SIGSTOP/SIGCONT with concurrent serializable range requests
  3. etcd panics: "unexpected error during txn: context canceled"
     at server/etcdserver/apply.go:638

Root cause: applyTxn() calls lg.Panic("unexpected error during txn")
when a range operation inside a serializable readonly transaction
returns a non-nil error. When the gRPC context is canceled (client
timeout during SIGSTOP), rangeKeys returns ctx.Err(), which
propagates to applyTxn where it triggers the fatal panic.
"""

import json as _json
import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_TRIGGER_SIDECAR_SCRIPT = r"""
apk add --no-cache curl >/dev/null 2>&1
# Wait for etcd to be healthy
until curl -sf http://127.0.0.1:2379/health >/dev/null 2>&1; do sleep 2; done

# Populate keys (idempotent — data survives restarts)
KEY1=$(echo -n 'key00001' | base64)
EXISTS=$(curl -sf http://127.0.0.1:2379/v3/kv/range -d "{\"key\":\"$KEY1\"}" 2>/dev/null | grep -c '"kvs"' || true)
if [ "$EXISTS" = "0" ]; then
  VALUE=$(head -c 10000 /dev/urandom | base64 | head -c 13000)
  VENC=$(echo -n "$VALUE" | base64 | tr -d '\n')
  for i in $(seq 1 2000); do
    KENC=$(echo -n "key$(printf '%05d' $i)" | base64 | tr -d '\n')
    curl -sf http://127.0.0.1:2379/v3/kv/put \
      -d "{\"key\":\"$KENC\",\"value\":\"$VENC\"}" >/dev/null 2>&1
  done
fi

# Trigger loop
while true; do
  until curl -sf http://127.0.0.1:2379/health >/dev/null 2>&1; do sleep 2; done
  sleep 5
  KS=$(echo -n 'key00001' | base64)
  KE=$(echo -n 'key02000' | base64)
  for j in $(seq 1 200); do
    curl -s --max-time 0.3 -X POST http://127.0.0.1:2379/v3/kv/txn \
      -d "{\"compare\":[],\"success\":[{\"request_range\":{\"key\":\"$KS\",\"range_end\":\"$KE\",\"serializable\":true}}],\"failure\":[]}" \
      >/dev/null 2>&1 &
  done
  sleep 0.1
  ETCD_PID=$(pgrep -x etcd 2>/dev/null | head -1)
  if [ -n "$ETCD_PID" ]; then
    kill -STOP "$ETCD_PID" 2>/dev/null
    sleep 2
    kill -CONT "$ETCD_PID" 2>/dev/null
  fi
  sleep 30
done
"""


class AutoEtcd14110(GenericCustomBuildProblem):
    db_name = "etcd"
    db_version = "3.5.5"
    source_git_ref = "v3.5.4"
    build_image = "golang:1.19"
    root_cause_description = (
        "etcd panics when processing serializable readonly transactions "
        "after a SIGSTOP/SIGCONT cycle. During SIGSTOP, client gRPC "
        "contexts expire. When the process resumes (SIGCONT), the "
        "applyTxn function in server/etcdserver/apply.go encounters a "
        "context.Canceled error from the range operation and calls "
        "lg.Panic('unexpected error during txn'). The bug is that "
        "applyTxn does not gracefully handle context cancellation — it "
        "treats ALL errors as unexpected and panics."
    )
    reproducer = (
        "#!/bin/sh\n"
        "etcdctl endpoint health 2>&1\n"
    )
    continuous_reproducer = True

    @mark_fault_injected
    def inject_fault(self):
        """Swap to buggy image and add sidecar that triggers SIGSTOP/SIGCONT."""
        ns = self.app.namespace
        sts_name = self.app.cluster_name

        logger.info(f"[etcd#14110] Swapping to buggy image: {self._custom_image}")
        self.app.inject_buggy_image(self._custom_image)

        logger.info("[etcd#14110] Adding trigger sidecar (SIGSTOP/SIGCONT loop)")
        patch = _json.dumps({
            "spec": {"template": {"spec": {
                "shareProcessNamespace": True,
                "containers": [{
                    "name": "trigger",
                    "image": "alpine:3.20",
                    "command": ["sh", "-c", _TRIGGER_SIDECAR_SCRIPT],
                }],
            }}}
        })
        subprocess.run(
            f"kubectl patch statefulset {sts_name} -n {ns} --type=strategic -p '{patch}'",
            shell=True, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            f"kubectl delete pod -n {ns} -l app.kubernetes.io/instance={sts_name} "
            f"--force --grace-period=0",
            shell=True, capture_output=True, text=True,
        )

        logger.info("[etcd#14110] Waiting for CrashLoopBackOff from sidecar trigger")
        self._wait_for_any_crash_loop(timeout=600)
        logger.info("[etcd#14110] CrashLoopBackOff confirmed — fault injected")

        self.app.deploy_continuous_reproducer(self.reproducer, self.expected_output)

    @mark_fault_injected
    def recover_fault(self):
        """Restore stock (fixed) image — v3.5.5 handles context cancellation."""
        logger.info("[etcd#14110] Restoring stock (fixed) image")
        self.app.restore_stock_image(custom_image=self._custom_image)

    def _wait_for_any_crash_loop(self, timeout: int = 600):
        """Wait until any etcd pod crashes (restartCount >= 1)."""
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
                    name = pod.get("metadata", {}).get("name", "")
                    if "reproducer" in name or "client" in name:
                        continue
                    for cs in pod.get("status", {}).get("containerStatuses", []):
                        if cs.get("name") != "etcd":
                            continue
                        reason = (
                            cs.get("state", {}).get("waiting", {}).get("reason", "")
                        )
                        exit_code = (
                            cs.get("state", {}).get("terminated", {}).get("exitCode")
                        )
                        restarts = cs.get("restartCount", 0)
                        if reason in ("CrashLoopBackOff", "Error") or (
                            exit_code is not None and exit_code != 0
                        ) or restarts >= 1:
                            logger.info(
                                f"Crash confirmed on '{name}': "
                                f"reason={reason or 'exit'} restarts={restarts}"
                            )
                            return
            except Exception:
                pass
            time.sleep(10)
        raise RuntimeError(
            f"Timeout ({timeout}s) waiting for etcd crash in '{ns}'"
        )
