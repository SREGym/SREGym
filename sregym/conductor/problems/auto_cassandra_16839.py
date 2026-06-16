"""CASSANDRA-16839: Truncation snapshots unnecessarily created on node startup.

Title: Unnecessary truncation snapshots created for size_estimates / table_estimates on every node start
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-16839

Buggy: cassandra 4.0.1   Fixed: cassandra 4.0.2 (also 3.0.26, 3.11.12, 4.1-alpha1, 4.1)

Reproduction summary (single node, NO workload required):
  On startup Cassandra clears the local size/table estimate tables
  (StorageService.cleanupSizeEstimates -> SystemKeyspace.clearAllEstimates, called from
  CassandraDaemon.setup). On the buggy 4.0.1 build that clear goes through the SNAPSHOTTING
  truncate path, so with auto_snapshot enabled (the default) each node start creates a fresh
  `truncated-<ts>-size_estimates` and `truncated-<ts>-table_estimates` snapshot. These accrue
  one new pair per node start (each with a distinct timestamp) and are visible via
  `nodetool listsnapshots`. The fix changes clearAllEstimates() to use
  truncateBlockingWithoutSnapshot(), so the fixed build shows "There are no snapshots".

  Boot the node, then run `nodetool listsnapshots`. The bug fires at the VERY FIRST boot,
  before any restart and on an empty system (the snapshots are of the empty estimate tables,
  hence the 0-bytes / 13-bytes-on-disk rows in the Jira report) — no CQL workload is needed.

Verbatim buggy signature (one literal `nodetool listsnapshots` row from buggy 4.0.1):
  truncated-1781238400142-size_estimates  system        size_estimates     0 bytes   13 bytes

Shape: nodetool sequence (not pure CQL). The bug is observed through `nodetool listsnapshots`
output, not a CQL query, and fires at node startup, so inject_fault() is overridden to swap in
the buggy image and then kubectl-exec `nodetool listsnapshots` (the cassandra_20108 / 17136
kubectl-exec pattern) rather than running CQL through cqlsh.

continuous_reproducer is intentionally False (diagnosis-only). The discriminator is the presence
of `truncated-*-{size,table}_estimates` snapshots, which are surfaced by nodetool/JMX, NOT by any
CQL query. The shared continuous-reproducer probe runs cqlsh against the cluster service and stays
Ready on both the buggy and the fixed build (it cannot list snapshots), so it would report "fixed"
while the bug is fully present. A diagnosis-only oracle (matching the cassandra_17136 / cassandra_20108
precedent) is correct here; a CQL-readiness mitigation oracle would be worse.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra16839(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    # 4.0.1 already ships the bug (fix landed in 4.0.2), so deploy the STOCK 4.0.1 image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/SystemKeyspace.java"
    root_cause_description = (
        "Cassandra creates an unnecessary truncation snapshot for system.size_estimates and "
        "system.table_estimates on every node start. At startup CassandraDaemon.setup() calls "
        "StorageService.cleanupSizeEstimates() -> SystemKeyspace.clearAllEstimates() to wipe the "
        "local estimate tables. On the buggy 4.0.1 build clearAllEstimates() truncates those tables "
        "via the SNAPSHOTTING truncate path, so with auto_snapshot enabled (the default) every node "
        "start takes a `truncated-<ts>-size_estimates` and `truncated-<ts>-table_estimates` snapshot "
        "of the (typically empty) estimate tables. These snapshots accumulate monotonically — one new "
        "pair, each with a distinct timestamp, per node start — and are visible via "
        "`nodetool listsnapshots`. The fix changes clearAllEstimates() to call "
        "truncateBlockingWithoutSnapshot() so no snapshot is taken for this internal cleanup."
    )

    # nodetool steps that EXPOSE the bug (no workload needed). Encoded for documentation / oracle
    # context; inject_fault() below executes the observation via kubectl exec (NOT as CQL through
    # cqlsh, which is what the default run_reproducer would do — the bug is not a CQL query).
    #
    # The bug fires at the very FIRST boot of the buggy node, before any restart. Restarting the
    # node simply adds one more `truncated-<ts>-{size,table}_estimates` pair per boot (distinct
    # timestamps), which is the evidence log's extra-rigor accumulation discriminator — the
    # single-boot snapshot pair already distinguishes buggy (pair present) from fixed (none).
    reproducer = """
# No CQL workload required. After the (buggy 4.0.1) node boots, list its snapshots:
nodetool listsnapshots

# BUGGY 4.0.1 -> a truncation-snapshot pair is present after the FIRST boot, e.g.:
#   truncated-1781238402833-table_estimates system        table_estimates    0 bytes   13 bytes
#   truncated-1781238400142-size_estimates  system        size_estimates     0 bytes   13 bytes
# FIXED 4.0.2 -> "There are no snapshots".

# (Optional accumulation check — one NEW pair per node start, each with a distinct <ts>.)
# In-place container restart so the data dir survives, then re-list:
#   kill 1            # restart the container in place (the data dir must survive the restart)
#   <wait for Ready>
#   nodetool listsnapshots   # buggy: 2 pairs after 2 boots, 3 pairs after 3 boots; fixed: still none
"""
    # Diagnosis-only — see the module docstring: the snapshot discriminator is nodetool/JMX, not
    # CQL, so the CQL-readiness mitigation probe cannot observe it (it would report "fixed" on the
    # buggy build). This matches the cassandra_17136 / cassandra_20108 precedent.
    continuous_reproducer = False

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, then OBSERVE the startup truncation-snapshot bug.

        This bug is exposed through `nodetool listsnapshots` (not a CQL query) and fires at node
        startup, so we override inject_fault() (rather than relying on the base class shoving the
        nodetool `reproducer` text through cqlsh as CQL). We swap in the buggy image — which triggers
        a node (re)start, on which 4.0.1 takes the unwanted truncated-* snapshots — then resolve one
        Cassandra pod and run `nodetool listsnapshots` on it to surface the signature.
        """
        if self._predeployed_buggy:
            logger.info("[Cassandra16839] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[Cassandra16839] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[Cassandra16839] Buggy image active")

        pod = self._first_cassandra_pod()
        if not pod:
            logger.warning("[Cassandra16839] No Cassandra pod found — cannot observe fault")
            return

        # Primary signature: the truncation-snapshot pair created at node startup. On buggy 4.0.1
        # this lists at least one `truncated-<ts>-size_estimates` + `truncated-<ts>-table_estimates`
        # pair; on the fixed build it prints "There are no snapshots".
        logger.info(f"[Cassandra16839] pod={pod}: nodetool listsnapshots (buggy 4.0.1 -> truncated-*-estimates pair)")
        out = self._exec_in_pod(pod, "nodetool listsnapshots")
        logger.info(f"[Cassandra16839] listsnapshots (post-buggy-start):\n{out[:1000]}")

        # Optional best-effort accumulation check: one NEW pair per node start. This is NOT required
        # to demonstrate the bug (the single-boot pair above already does) and is therefore best-effort
        # only. CAVEAT: on the K8ssandra management-api container PID 1 is the management API, not bare
        # `cassandra -f`, so `kill 1` semantics differ from the bare-pod reproduction in the evidence
        # log; and the operator-managed data volume must survive the restart for snapshots to accrue.
        logger.info(
            f"[Cassandra16839] (best-effort) in-place restart via kill 1 to check snapshot accumulation on pod {pod}"
        )
        self._exec_in_pod(pod, "kill 1")
        subprocess.run(
            f"kubectl wait pod/{pod} -n {self.namespace} --for=condition=Ready --timeout=300s",
            shell=True,
            capture_output=True,
            text=True,
        )
        out2 = self._exec_in_pod(pod, "nodetool listsnapshots")
        logger.info(
            f"[Cassandra16839] (best-effort) listsnapshots (after one in-place restart; "
            f"buggy expects an ADDITIONAL truncated-*-estimates pair):\n{out2[:1000]}"
        )

    @mark_fault_injected
    def recover_fault(self):
        """Restore the stock image and wait for the cluster to be Ready."""
        logger.info("[Cassandra16839] Recovering: restoring cluster to stock image")
        self.app.restore_stock_image(custom_image=self._custom_image)
        logger.info("[Cassandra16839] Recovery complete")

    # ── helpers ────────────────────────────────────────────────────────────────

    def _first_cassandra_pod(self) -> str:
        """Return the name of one Cassandra pod for this cluster (or "" if none)."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        pods = [p.strip() for p in out.splitlines() if p.strip()]
        return pods[0] if pods else ""

    def _exec_in_pod(self, pod: str, command: str) -> str:
        """Run a shell command in the cassandra container of `pod` and return combined output.
        `nodetool` / `kill` returning non-zero is logged rather than raised."""
        result = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -c {self._shquote(command)}",
            shell=True,
            capture_output=True,
            text=True,
        )
        combined = (result.stdout + result.stderr).strip()
        logger.info(f"[Cassandra16839] exec rc={result.returncode}: {combined[:400]}")
        return combined

    @staticmethod
    def _shquote(s: str) -> str:
        """Single-quote a string for safe embedding in a shell command line."""
        return "'" + s.replace("'", "'\\''") + "'"
