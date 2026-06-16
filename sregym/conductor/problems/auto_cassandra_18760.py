"""CASSANDRA-18760: Backport CASSANDRA-16905 — type-incompatible re-add of a dropped column is not blocked.

Title: Backport CASSANDRA-16905 to older branches
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-18760

Buggy: cassandra 4.0.11   Fixed: cassandra 4.0.12 (also 3.0.30, 3.11.16)

Reproduction (single node; schema + a local sstable, no ring divergence needed):
  1. CREATE a table with a `col1 map<int, tinyint>` column and INSERT map cells with a
     far-future write timestamp (USING TIMESTAMP 9223372036854775000). The far-future
     timestamp defeats DROP read-shadowing so the cells physically survive the later DROP
     and are read back under the re-added column.
  2. `nodetool flush` — flush the map cells to an sstable so they survive on disk (load-bearing:
     the bug surfaces when the surviving on-disk complex/map cells are read under the new
     simple/blob column).
  3. `ALTER TABLE ... DROP col1;` then `ALTER TABLE ... ADD col1 blob;`. On 4.0.11 the
     type-incompatible map->blob re-add SUCCEEDS (the CASSANDRA-16905 guardrail is missing),
     leaving a contradictory schema: col1 is live as `blob` while system_schema.dropped_columns
     still records col1 as `map<int, tinyint>`.
  4. `SELECT *` reads the surviving complex/map cells under the simple/blob column and fails
     server-side; the client sees a ReadFailure (code=1300).

The fixed image (4.0.12) BLOCKS step 3's `ADD col1 blob` at ALTER time with
`InvalidRequest ... Cannot re-add previously dropped column 'col1' of type blob, incompatible
with previous type map<int, tinyint>`, so the corrupt-schema state is never created.

*** VERBATIM BUGGY SIGNATURE *** (server-side, from `kubectl logs`):
  ERROR [ReadStage-13] ... AbstractLocalAwareExecutorService.java:169 - Uncaught exception on thread Thread[ReadStage-13,10,main]
  java.lang.AssertionError: col1
      at org.apache.cassandra.db.rows.UnfilteredSerializer.lambda$serializeRowBody$0(UnfilteredSerializer.java:244)
      at org.apache.cassandra.db.rows.UnfilteredSerializer.serializeRowBody(UnfilteredSerializer.java:237)
      at org.apache.cassandra.db.rows.UnfilteredSerializer.serialize(UnfilteredSerializer.java:205)

Shape: nodetool/flush sequence (not pure CQL). The load-bearing `nodetool flush` cannot run
through the cqlsh-only run_reproducer, so inject_fault() is overridden to kubectl-exec the CQL
setup, flush every node, run the schema ALTERs, then fire the failing SELECT (the 17136/20108
pattern).

continuous_reproducer is intentionally False (diagnosis-only). Per this bug's reproduction
evidence log, `SELECT *` returns a ReadFailure on BOTH the buggy (4.0.11) and the fixed (4.0.12)
node — the far-future-timestamp map cell physically survives the DROP on both, so a fixed node
ALSO throws on the read (a benign IllegalStateException at Columns.java:593, vs the buggy
AssertionError at UnfilteredSerializer.java:244). The cassandra continuous-reproducer probe only
checks the cqlsh exit code, which is non-zero on BOTH versions, so it could never signal "fixed":
a mitigation oracle here would be silently broken. The real discriminator (the ALTER ADD being
allowed vs rejected) is one-shot/stateful and the buggy-vs-fixed signal is server-log-only —
invisible to a client cqlsh probe. A diagnosis-only oracle (matching cassandra_20108 / auto
17136) is the correct, honest choice.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra18760(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.11"
    source_git_ref = "cassandra-4.0.11"
    # 4.0.11 already ships the bug (fix lands in 4.0.12), so deploy the stock image
    # instead of an ant-jar source build.
    prebuilt_from_stock = True

    # Root cause is the MISSING guardrail in the ALTER TABLE ... ADD validation path
    # (CASSANDRA-16905, backported by this ticket), NOT the UnfilteredSerializer assertion,
    # which is only where the resulting corruption surfaces on read.
    root_cause_file = "src/java/org/apache/cassandra/cql3/statements/schema/AlterTableStatement.java"
    root_cause_description = (
        "ALTER TABLE ... ADD on 4.0.11 does not validate a re-added column against the type it "
        "had when previously dropped. The CASSANDRA-16905 guardrail (backported by CASSANDRA-18760) "
        "is missing, so re-adding a previously-dropped `map<int, tinyint>` column as `blob` "
        "succeeds even though the types are incompatible. This leaves a contradictory schema: col1 "
        "is live as `blob` while system_schema.dropped_columns still records col1 as "
        "`map<int, tinyint>`. When a SELECT then reads the map cells that physically survived the "
        "DROP (written with a far-future timestamp) under the now-`blob` column, serialization of "
        "the complex/map cells as a simple column fails with `java.lang.AssertionError: col1` in "
        "UnfilteredSerializer.serializeRowBody (UnfilteredSerializer.java:244), surfacing to the "
        "client as a ReadFailure; scrub/compact cannot recover the data (they silently drop it). "
        "The fix blocks the type-incompatible re-add at ALTER time with InvalidRequest 'Cannot "
        "re-add previously dropped column ... incompatible with previous type', preventing the "
        "corruption entirely."
    )

    # The full buggy reproduction sequence, encoded for documentation/diagnosis context.
    # inject_fault() below executes these steps via kubectl exec (the CQL via in-pod cqlsh and the
    # load-bearing `nodetool flush` via nodetool) — NOT through the default cqlsh-only
    # run_reproducer, which cannot run nodetool. Keyspace RF and per-node flush are handled in
    # inject_fault() because the shared Cassandra DBBuildSpec deploys a 3-node DC.
    reproducer = """
-- Far-future write timestamp so the map cells survive the later DROP and are read
-- back under the re-added column (defeats DROP read-shadowing).
CREATE KEYSPACE IF NOT EXISTS repro18760
  WITH REPLICATION = {'class': 'NetworkTopologyStrategy', 'dc1': 3};
CREATE TABLE repro18760.t2 (pk int PRIMARY KEY, col1 map<int, tinyint>);
INSERT INTO repro18760.t2 (pk, col1) VALUES (1, {10:1, 20:2, 30:3}) USING TIMESTAMP 9223372036854775000;
INSERT INTO repro18760.t2 (pk, col1) VALUES (2, {40:4, 50:5}) USING TIMESTAMP 9223372036854775000;

-- nodetool flush repro18760 t2   (run on every node; load-bearing — cells must be on disk)

-- The type-incompatible map->blob re-add SUCCEEDS on 4.0.11 (missing CASSANDRA-16905 guardrail),
-- leaving col1 live as blob while dropped_columns records it as map<int, tinyint>.
ALTER TABLE repro18760.t2 DROP col1;
ALTER TABLE repro18760.t2 ADD col1 blob;

-- Reads the surviving map cells under the blob column -> server-side AssertionError: col1
-- (UnfilteredSerializer.java:244) -> client-visible ReadFailure (code=1300).
SELECT * FROM repro18760.t2;
"""

    # Diagnosis-only: see the module docstring — SELECT * errors on BOTH buggy and fixed nodes
    # (the far-future-timestamp cell survives the DROP on both), so a cqlsh-exit-code probe can
    # never signal "fixed". The buggy-vs-fixed difference is server-log-only.
    continuous_reproducer = False

    # ── CQL / nodetool steps (run in-pod by inject_fault) ────────────────────────

    # Schema + data with a far-future timestamp so the cells survive the DROP.
    _SETUP_CQL = (
        "CREATE KEYSPACE IF NOT EXISTS repro18760 "
        "WITH REPLICATION = {'class': 'NetworkTopologyStrategy', 'dc1': 3}; "
        "CREATE TABLE IF NOT EXISTS repro18760.t2 (pk int PRIMARY KEY, col1 map<int, tinyint>); "
        "INSERT INTO repro18760.t2 (pk, col1) VALUES (1, {10:1, 20:2, 30:3}) "
        "USING TIMESTAMP 9223372036854775000; "
        "INSERT INTO repro18760.t2 (pk, col1) VALUES (2, {40:4, 50:5}) "
        "USING TIMESTAMP 9223372036854775000;"
    )

    # The buggy schema mutation: drop the map column, then re-add it as blob. On 4.0.11 the
    # ADD blob SUCCEEDS (no guardrail) and leaves the contradictory live-blob/dropped-map schema.
    _ALTER_CQL = "ALTER TABLE repro18760.t2 DROP col1; ALTER TABLE repro18760.t2 ADD col1 blob;"

    # The read that trips the bug — surfaces the on-disk corruption as AssertionError: col1
    # server-side and a ReadFailure client-side.
    _FAILING_SELECT = "SELECT * FROM repro18760.t2;"

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, then drive the map->blob re-add corruption.

        This bug needs a load-bearing `nodetool flush` between the INSERT and the ALTERs, which
        the default cqlsh-only run_reproducer cannot do, so we override inject_fault() and drive
        every step via kubectl exec (the 17136/20108 pattern).

        The shared Cassandra DBBuildSpec deploys a 3-node DC, not the single node in the evidence
        log. To make the flush load-bearing regardless of which replica owns the partitions, the
        keyspace is created with RF=3 (every node holds the cells) and `nodetool flush` is run on
        EVERY cassandra pod. The schema ALTERs and the final SELECT go through one pod (schema is
        cluster-wide and the SELECT only needs to land on a replica that has the surviving cells).
        """
        if self._predeployed_buggy:
            logger.info("[Cassandra18760] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[Cassandra18760] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[Cassandra18760] Buggy image active")

        pods = self._cassandra_pods()
        if not pods:
            logger.warning("[Cassandra18760] No Cassandra pod found — cannot inject fault")
            return
        pod = pods[0]

        # Step 1: create the keyspace/table and write the map cells with a far-future timestamp.
        logger.info(f"[Cassandra18760] Creating schema + writing far-future-timestamp map cells on pod {pod}")
        self._cqlsh(pod, self._SETUP_CQL)

        # Step 2: flush the map cells to an sstable on every node (load-bearing — the cells must
        # be on disk so they survive the DROP and are read under the re-added blob column).
        for p in pods:
            logger.info(f"[Cassandra18760] Flushing repro18760.t2 on pod {p}")
            self._exec_in_pod(p, "nodetool flush repro18760 t2")

        # Step 3: drop col1 (map) and re-add it as blob. On 4.0.11 the type-incompatible re-add
        # SUCCEEDS (missing CASSANDRA-16905 guardrail) and leaves the corrupt schema. On a fixed
        # node the ADD blob would be rejected with InvalidRequest (which is the intended fix).
        logger.info(f"[Cassandra18760] Dropping col1 (map) and re-adding it as blob on pod {pod}")
        self._cqlsh(pod, self._ALTER_CQL)

        # Step 4: read the surviving map cells under the blob column. Server-side this throws
        # `java.lang.AssertionError: col1` (UnfilteredSerializer.java:244); the client sees a
        # ReadFailure. cqlsh exiting non-zero here is EXPECTED, not a failure.
        logger.info(f"[Cassandra18760] Firing failing SELECT on pod {pod} (expect ReadFailure / AssertionError: col1)")
        self._cqlsh(pod, self._FAILING_SELECT)
        logger.info("[Cassandra18760] Reproducer issued — AssertionError: col1 expected in the server log")

    @mark_fault_injected
    def recover_fault(self):
        """Restore the stock image and wait for the cluster to be Ready."""
        logger.info("[Cassandra18760] Recovering: restoring cluster to stock image")
        self.app.restore_stock_image(custom_image=self._custom_image)
        logger.info("[Cassandra18760] Recovery complete")

    # ── helpers ──────────────────────────────────────────────────────────────────

    def _cassandra_pods(self) -> list[str]:
        """Return the names of all Cassandra pods for this cluster (empty list if none)."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        return [p.strip() for p in out.splitlines() if p.strip()]

    def _cqlsh(self, pod: str, cql: str) -> None:
        """Run a CQL block via in-pod cqlsh -e. The failing SELECT exits non-zero (ReadFailure),
        which is expected for this trigger, so non-zero exit is logged rather than raised."""
        self._exec_in_pod(pod, f"cqlsh -e {self._shquote(cql)}")

    def _exec_in_pod(self, pod: str, command: str) -> None:
        """Run a shell command in the cassandra container of `pod`. Both `nodetool flush` and the
        failing SELECT can exit non-zero as part of the reproduction, so failures are logged
        rather than raised."""
        command = self._authed_cqlsh(command)
        result = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -c {self._shquote(command)}",
            shell=True,
            capture_output=True,
            text=True,
        )
        combined = (result.stdout + result.stderr).strip()
        logger.info(f"[Cassandra18760] exec rc={result.returncode}: {combined[:400]}")

    @staticmethod
    def _shquote(s: str) -> str:
        """Single-quote a string for safe embedding in a shell command line."""
        return "'" + s.replace("'", "'\\''") + "'"
