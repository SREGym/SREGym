"""CASSANDRA-21065: nodetool garbagecollect throws ConcurrentModificationException.

JIRA:    https://issues.apache.org/jira/browse/CASSANDRA-21065
Buggy:   5.0.6   ->   Fixed: 5.0.7 (control build 5.0.8 also runs clean)
Component: Tool/nodetool, Local/Compaction (garbage collection / single-sstable compaction)

Reproduction summary (single node, RF=1):
  1. CREATE KEYSPACE k (RF=1) + CREATE TABLE k.t with compaction =
     {'class':'UnifiedCompactionStrategy','only_purge_repaired_tombstones':'true','scaling_parameters':'L10'}.
  2. nodetool disableautocompaction k t   (so the sstables are NOT auto-merged away).
  3. INSERT + DELETE the SAME partition key, then nodetool flush k t -- repeat >= 2 TIMES so the
     owning node accumulates >= 2 UNREPAIRED sstables (a single sstable does NOT trip the bug, and
     only_purge_repaired_tombstones='true' means the sstables must remain unrepaired -- never repair).
  4. nodetool garbagecollect k t  -> throws java.util.ConcurrentModificationException.

The discriminator is purely server-side (a nodetool/JMX operation), so it is driven on the Cassandra
server pod via kubectl exec; there is no pure-CQL probe a client pod could run to detect it.

VERBATIM BUGGY SIGNATURE (5.0.6, from the reproduction evidence log):
  java.util.ConcurrentModificationException
    at java.util.Collections$UnmodifiableCollection$1.next
    at org.apache.cassandra.db.compaction.CompactionManager$6.filterSSTables(CompactionManager.java:691)
    at ...performGarbageCollection(CompactionManager.java:683)

Fixed 5.0.8 control runs the identical workload clean.

Root cause (per-node, single-node local compaction logic):
  CompactionManager.filterSSTables (the anonymous CompactionTask used by performGarbageCollection)
  iterates transaction.originals() while calling transaction.cancel() on each unrepaired sstable in
  the same loop. cancel() mutates the underlying originals set that the iterator is walking, so the
  next iterator.next() throws ConcurrentModificationException. The bug only surfaces when >= 2
  unrepaired sstables are present (with only_purge_repaired_tombstones='true' garbagecollect cancels
  the unrepaired ones), which is why the reproduction needs a flush-per-iteration loop.

Reproduction shape: nodetool / flush sequence (NOT a pure-CQL continuous reproducer).
  The bug only fires from `nodetool garbagecollect` after >= 2 unrepaired sstables exist on a single
  node, which requires `nodetool disableautocompaction` + a `nodetool flush` between each INSERT/DELETE,
  plus the `nodetool garbagecollect` trigger itself. The framework's CQL-only reproducer/mitigation
  path (a separate cassandra:4.1 client pod that only pipes CQL into cqlsh) CANNOT run nodetool. So,
  per the SREGym decision tree, inject_fault() is overridden to drive the full disableautocompaction +
  (INSERT/DELETE + flush) x N + garbagecollect sequence via kubectl-exec on the server pod, and
  continuous_reproducer is left False (diagnosis-only, mitigation_oracle = None), like the other
  server-side Cassandra bug problems (e.g. auto_cassandra_20313, auto_cassandra_20036).

Node co-location note (3-node deploy at RF=1):
  The K8ssandra deploy is a 3-node datacenter; RF=1 scatters partitions by key. To guarantee the >= 2
  sstables land on the SAME node, the INSERT/DELETE loop reuses ONE partition key (pk=1) every
  iteration, so both flushes produce sstables on pk=1's single owning replica -> that node accumulates
  the 2 unrepaired sstables. disableautocompaction/flush/garbagecollect are NODE-LOCAL and are run on
  EVERY node (non-owner flush/gc no-op; the owner trips the CME). No `nodetool repair` is ever run --
  only_purge_repaired_tombstones='true' requires the sstables to stay unrepaired for the bug to fire.
"""

import base64
import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KEYSPACE = "k"
_TABLE = "t"
# Reuse the SAME partition key every iteration so both flushed sstables land on the one node that
# owns pk=1 (RF=1 on a 3-node ring scatters partitions by key; same key => same owner => >= 2 sstables).
_PK = 1
# Number of INSERT/DELETE + flush iterations. >= 2 unrepaired sstables are required to trip the bug;
# 3 gives margin.
_ITERATIONS = 3


class AutoCassandra21065(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.6"
    source_git_ref = "cassandra-5.0.6"
    # 5.0.6 already ships the bug (fix landed in 5.0.7), so deploy the stock image instead of
    # running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/compaction/CompactionManager.java"
    root_cause_description = (
        "nodetool garbagecollect throws java.util.ConcurrentModificationException when a table has "
        ">= 2 unrepaired sstables. In CompactionManager (the anonymous CompactionTask driven by "
        "performGarbageCollection -> filterSSTables at CompactionManager.java:691/683), the code "
        "iterates transaction.originals() while calling transaction.cancel() on each unrepaired sstable "
        "inside the same loop. cancel() mutates the underlying originals set the iterator is walking, so "
        "the next iterator.next() over the unmodifiable view of that set throws "
        "ConcurrentModificationException (Collections$UnmodifiableCollection$1.next). With "
        "only_purge_repaired_tombstones='true', garbagecollect cancels the unrepaired sstables, which is "
        "why >= 2 unrepaired sstables are needed to expose the concurrent-modification-during-iteration "
        "bug; a single sstable does not trip it. The fix iterates over a snapshot/copy of the sstables "
        "instead of cancelling entries while iterating the live originals set."
    )

    # Canonical buggy steps (documentation / human-readable record). The actual orchestration -
    # disableautocompaction, the (INSERT/DELETE + nodetool flush) loop, and the nodetool garbagecollect
    # trigger - is driven by inject_fault() below, because the nodetool steps are NOT expressible as a
    # CQL string piped into cqlsh.
    reproducer = """
-- Setup (run via cqlsh on the server pod):
CREATE KEYSPACE IF NOT EXISTS k WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS k.t (pk int PRIMARY KEY, v text) WITH compaction =
 {'class':'UnifiedCompactionStrategy','only_purge_repaired_tombstones':'true','scaling_parameters':'L10'};

-- Stop auto-compaction so the sstables are not merged away (run via nodetool on the server pod):
--   nodetool disableautocompaction k t

-- Build >= 2 UNREPAIRED sstables on the owning node: repeat >= 2 TIMES, reusing the SAME partition key.
-- Each iteration is INSERT + DELETE, then a nodetool flush so each iteration produces its OWN sstable:
INSERT INTO k.t (pk, v) VALUES (1, 'x');
DELETE FROM k.t WHERE pk = 1;
--   nodetool flush k t          <-- sstable #1
INSERT INTO k.t (pk, v) VALUES (1, 'x');
DELETE FROM k.t WHERE pk = 1;
--   nodetool flush k t          <-- sstable #2  (>= 2 unrepaired sstables now exist)
-- (NEVER run `nodetool repair` -- only_purge_repaired_tombstones='true' needs them UNREPAIRED.)

-- Trigger the bug (run via nodetool on the server pod):
--   nodetool garbagecollect k t
-- Buggy 5.0.6: throws java.util.ConcurrentModificationException
--     at java.util.Collections$UnmodifiableCollection$1.next
--     at org.apache.cassandra.db.compaction.CompactionManager$6.filterSSTables(CompactionManager.java:691)
--     at ...performGarbageCollection(CompactionManager.java:683)
-- Fixed 5.0.7/5.0.8: garbagecollect completes cleanly.
"""
    # Server-side (nodetool/garbagecollect) bug: there is NO pure-CQL probe the CQL-only reproducer pod
    # can run to detect it (it cannot run nodetool), so this is diagnosis-only. Setting
    # continuous_reproducer True here would deploy a mitigation pod that pipes CQL into cqlsh and could
    # never observe the garbagecollect CME, leaving it permanently NotReady - worse than no mitigation
    # oracle. (Mirrors auto_cassandra_20313 / auto_cassandra_20036.)
    continuous_reproducer = False
    # No expected_output: this is an exception/error bug, not a wrong-result bug.

    # ── CQL/nodetool driven on the server pod ─────────────────────────────────

    _SETUP_CQL = (
        f"CREATE KEYSPACE IF NOT EXISTS {_KEYSPACE} "
        "WITH replication = {'class':'SimpleStrategy','replication_factor':1}; "
        f"CREATE TABLE IF NOT EXISTS {_KEYSPACE}.{_TABLE} (pk int PRIMARY KEY, v text) WITH compaction = "
        "{'class':'UnifiedCompactionStrategy','only_purge_repaired_tombstones':'true',"
        "'scaling_parameters':'L10'};"
    )

    # One INSERT/DELETE pair against the SAME partition key (so all sstables co-locate on its owner).
    _MUTATE_CQL = (
        f"INSERT INTO {_KEYSPACE}.{_TABLE} (pk, v) VALUES ({_PK}, 'x'); "
        f"DELETE FROM {_KEYSPACE}.{_TABLE} WHERE pk = {_PK};"
    )

    def _server_pods(self) -> list[str]:
        """Return ALL Running cass-operator-managed Cassandra server pods.

        The cluster is deployed by the K8ssandra/cass-operator (see _cassandra_cluster_manifest),
        which labels server pods with ``app.kubernetes.io/name=cassandra``. disableautocompaction,
        flush and garbagecollect are NODE-LOCAL, so they are run on every node: the node that owns
        pk=1 accumulates the >= 2 unrepaired sstables and trips the CME; the others no-op.
        """
        out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} "
                f"-l app.kubernetes.io/name=cassandra "
                f"--field-selector=status.phase=Running "
                f"-o jsonpath='{{.items[*].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return [p for p in out.split() if p]

    def _superuser_creds(self) -> tuple[str, str]:
        """Read the K8ssandra-managed superuser credentials from the cluster secret.

        K8ssandra enables PasswordAuthenticator by default and generates a
        ``<cluster_name>-superuser`` secret; fall back to cassandra/cassandra.
        """
        secret = f"{self.app.cluster_name}-superuser"
        u = (
            subprocess.run(
                f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.username}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        p = (
            subprocess.run(
                f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.password}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return (
            base64.b64decode(u).decode() if u else "cassandra",
            base64.b64decode(p).decode() if p else "cassandra",
        )

    def _exec_cql(self, pod: str, u_b64: str, p_b64: str, cql: str, timeout: int = 180) -> subprocess.CompletedProcess:
        """Pipe CQL into cqlsh inside the ``cassandra`` container of ``pod``, with superuser creds."""
        return subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f'cqlsh -u "$U" -p "$P" --request-timeout=60'
            f"'",
            shell=True,
            capture_output=True,
            text=True,
            input=cql,
            timeout=timeout,
        )

    def _exec_nodetool(
        self, pod: str, u_b64: str, p_b64: str, args: str, timeout: int = 180
    ) -> subprocess.CompletedProcess:
        """Run ``nodetool <args>`` on the server pod.

        cass-management-api nodetool needs the JMX superuser creds; pass them best-effort and fall
        back to a bare invocation (same trick as auto_cassandra_20313 / auto_cassandra_20086).
        """
        return subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f'nodetool -u "$U" -pw "$P" {args} || nodetool {args}'
            f"'",
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @mark_fault_injected
    def inject_fault(self):
        """Drive the CASSANDRA-21065 garbagecollect reproduction on the server pod(s).

        Steps (all on the buggy 5.0.6 server pods, so the buggy build itself runs the compaction):
          1. Ensure the buggy image is active (no-op when prebuilt_from_stock pre-deployed it).
          2. CREATE KEYSPACE/TABLE with UnifiedCompactionStrategy + only_purge_repaired_tombstones=true.
          3. nodetool disableautocompaction k t on EVERY node (so the sstables are not auto-merged).
          4. Repeat _ITERATIONS times: INSERT + DELETE pk=1 (one coordinator), then nodetool flush k t
             on EVERY node -> each iteration adds one UNREPAIRED sstable on pk=1's owning node.
          5. nodetool garbagecollect k t on EVERY node -> the owner (>= 2 unrepaired sstables) throws
             ConcurrentModificationException; the others no-op. NO `nodetool repair` is ever run.
        """
        # 1. Make sure the buggy binary is the one running (lifecycle parity with the base class).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra21065] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra21065] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra21065] Buggy image active")

        self.setup_preconditions()

        pods = self._server_pods()
        if not pods:
            logger.warning("[AutoCassandra21065] No running Cassandra server pod found — cannot run reproducer")
            return
        coordinator = pods[0]  # any node can coordinate the INSERT/DELETE; the operator routes by key
        logger.info(f"[AutoCassandra21065] Server pods: {pods} (coordinator={coordinator})")

        try:
            username, password = self._superuser_creds()
            u_b64 = base64.b64encode(username.encode()).decode()
            p_b64 = base64.b64encode(password.encode()).decode()

            # 2. Schema with the verbatim compaction options from the evidence log.
            logger.info(
                "[AutoCassandra21065] Creating keyspace/table (UnifiedCompactionStrategy, only_purge_repaired_tombstones=true)"
            )
            self._exec_cql(coordinator, u_b64, p_b64, self._SETUP_CQL)
            time.sleep(3)  # let the schema settle before mutating/flushing

            # 3. Disable auto-compaction node-locally on EVERY node so the per-iteration sstables
            #    survive (the owner must have it off so its 2 sstables are not merged into one).
            logger.info(
                f"[AutoCassandra21065] nodetool disableautocompaction {_KEYSPACE} {_TABLE} on all {len(pods)} node(s)"
            )
            for pod in pods:
                self._exec_nodetool(pod, u_b64, p_b64, f"disableautocompaction {_KEYSPACE} {_TABLE}")

            # 4. INSERT/DELETE the SAME pk + flush ALL nodes, INSIDE the loop (one flush == one sstable;
            #    flushing only once after all mutations would yield a single sstable and NOT trip the bug).
            for i in range(1, _ITERATIONS + 1):
                logger.info(
                    f"[AutoCassandra21065] iteration {i}/{_ITERATIONS}: INSERT+DELETE pk={_PK}, then flush all nodes"
                )
                self._exec_cql(coordinator, u_b64, p_b64, self._MUTATE_CQL)
                time.sleep(1)
                for pod in pods:
                    self._exec_nodetool(pod, u_b64, p_b64, f"flush {_KEYSPACE} {_TABLE}")
                time.sleep(1)

            # 5. garbagecollect on EVERY node: the owner (>= 2 unrepaired sstables) trips the CME.
            logger.info(
                f"[AutoCassandra21065] nodetool garbagecollect {_KEYSPACE} {_TABLE} on all node(s) (expect ConcurrentModificationException on 5.0.6)"
            )
            tripped = False
            for pod in pods:
                result = self._exec_nodetool(pod, u_b64, p_b64, f"garbagecollect {_KEYSPACE} {_TABLE}", timeout=240)
                combined = (result.stdout + result.stderr).strip()
                if "ConcurrentModificationException" in combined:
                    tripped = True
                    logger.info(
                        f"[AutoCassandra21065] BUGGY SIGNATURE on pod {pod}: "
                        f"garbagecollect threw ConcurrentModificationException\n{combined[:500]}"
                    )
                elif result.returncode != 0:
                    logger.info(
                        f"[AutoCassandra21065] garbagecollect non-zero on pod {pod} (rc={result.returncode}): {combined[:300]}"
                    )
            if not tripped:
                logger.warning(
                    "[AutoCassandra21065] garbagecollect did not surface ConcurrentModificationException on any node "
                    "(check that >= 2 unrepaired sstables co-located on pk's owner; the bug needs >= 2 unrepaired sstables)"
                )
        except subprocess.TimeoutExpired:
            # A hung garbagecollect is itself an abnormal outcome; the CME surfaces in the server log regardless.
            logger.info(
                "[AutoCassandra21065] a nodetool step timed out (the CME surfaces in the server log regardless)"
            )
        except Exception as e:  # tolerate exec hiccups; the bug surfaces in the server log regardless
            logger.warning(f"[AutoCassandra21065] inject_fault raised (continuing): {e}")
