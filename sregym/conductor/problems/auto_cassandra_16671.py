"""CASSANDRA-16671: Cassandra can return no row when the row columns have been deleted.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16671

Buggy: 3.11.10  ->  Fixed: 3.11.11 (fix commit 24346d17899df8610a5f425c7074ddd5dc8082bb).
Component: Legacy/Local Write-Read Paths.

Reproduced on a raw 2-node ring (RF=2) through ``CassandraRawRingProblem`` — see steps below.

Reproduction summary (2-node ring, RF=2 SimpleStrategy, local read-path bug):
  On a two-node ring with RF=2, per-replica divergence is injected via gossip isolation
  (clean bidirectional DN) so each CONSISTENCY ONE write lands on exactly one replica,
  with hinted handoff disabled so the divergence is preserved. cass-0 gets
  INSERT(pk=1,ck='1',v=1) USING TIMESTAMP 1000 -> flush -> UPDATE SET v=2 USING TIMESTAMP
  2000 -> flush (TWO sstables: the INSERT sstable carries PK liveness; the all-columns
  UPDATE sstable carries NO row liveness). cass-1 gets DELETE v USING TIMESTAMP 3000 ->
  flush (a column tombstone only). After gossip is re-enabled (both UN), a read coordinated
  from cass-1 at `CONSISTENCY ALL; SELECT * FROM ks16671.tbl WHERE pk=1 AND ck='1'` must
  return `row(1, '1', null)` per CQL semantics (a row exists while it has one non-null
  column, incl. PK columns). The buggy 3.11.10 WRONGLY returns (0 rows): cass-0's local
  timestamp-ordered read stops early on the all-columns UPDATE sstable, dropping the row's
  PK liveness; merged with cass-1's column deletion the coordinator then drops the whole row.

This bug is a per-replica-divergence + local-read-path regression (from CASSANDRA-16226);
it CANNOT manifest on a single node. It requires multi-pod orchestration (per-node gossip
isolation + per-node CL ONE writes + per-node flush so cass-0 holds two divergent sstables
and cass-1 holds a column tombstone). That orchestration is realised here on a raw 2-node
ring through ``CassandraRawRingProblem``: ``deploy_app`` stands up a stock ``cassandra:3.11.10``
ring, ``inject_fault`` builds the per-replica divergence via gossip isolation, and the
``CassandraWrongResultOracle`` re-establishes it and runs the coordinator ``SELECT *``,
grading the bug present when it returns ``(0 rows)`` (buggy 3.11.10) vs ``1 | 1 | null``
``(1 rows)`` (fixed 3.11.11). See the authoritative evidence log:
.claude/repro-evidence/repro-CASSANDRA-16671.md

Verbatim buggy signature (cassandra:3.11.10, coordinator cass-1, CL=ALL, FIRST read):
  Consistency level set to ALL.

   pk | ck | v
  ----+----+---


  (0 rows)

Contrast on the SAME buggy node — `SELECT v` returns 1 row (null), proving the row data is
present and only `SELECT *` regresses:
  Consistency level set to ALL.

   v
  ------
   null

  (1 rows)

Fixed 3.11.11 returns, for the identical workload, `SELECT *` => `1 | 1 | null` (1 rows).
"""

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraWrongResultOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

# Keyspace/table and the divergence pattern from the evidence log (see module docstring).
_KS = "ks16671"
_TABLE = f"{_KS}.tbl"
_MONEY_QUERY = f"SELECT * FROM {_TABLE} WHERE pk = 1 AND ck = '1';"


class AutoCassandra16671(CassandraRawRingProblem):
    """Local-read-path wrong-result bug (row wrongly dropped), reproduced on a raw 2-node ring.

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:3.11.10`` ring; ``inject_fault`` creates the schema and per-replica divergence
    via gossip isolation (cass-0 = INSERT@ts1000 + all-columns UPDATE@ts2000 in two sstables;
    cass-1 = column DELETE@ts3000 tombstone); the ``CassandraWrongResultOracle`` re-establishes
    fresh divergence and runs ``SELECT * ... WHERE pk=1 AND ck='1'`` from coordinator cass-1 at
    CONSISTENCY ALL, grading the bug present when it returns ``(0 rows)`` (buggy 3.11.10) vs
    ``1 | 1 | null`` ``(1 rows)`` (fixed 3.11.11).
    """

    db_name = "cassandra"
    db_version = "3.11.10"
    cassandra_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    ring_namespace = "cassraw-16671"
    replicas = 2

    root_cause_file = "src/java/org/apache/cassandra/db/SinglePartitionReadCommand.java"
    root_cause_description = (
        "Cassandra wrongly returns no row (0 rows) for a `SELECT *` when a row's non-PK "
        "columns have been deleted on one replica while another replica holds the row via an "
        "all-columns UPDATE. This is a local read-path regression introduced by CASSANDRA-16226 "
        "in SinglePartitionReadCommand.java: the timestamp-ordered read stops EARLY when an "
        "UPDATE covering all requested columns is found in an SSTable. CQL semantics say a row "
        "exists as long as it has one non-null column INCLUDING primary-key columns: INSERT sets "
        "the row's primary-key liveness, UPDATE does NOT. cass-0's two sstables are an INSERT (PK "
        "liveness @ts1000) and an all-columns UPDATE (v=2 @ts2000, NO row liveness); the "
        "early-stop returns the row from the UPDATE sstable carrying no PK liveness and never "
        "reaches the older INSERT sstable that holds it. Merged with cass-1's column DELETE "
        "(tombstone @ts3000) the coordinator then sees a row with no live cell AND no PK liveness "
        "and drops it entirely, returning 0 rows instead of the correct `row(pk, ck, null)`. "
        "(`SELECT v` of the single column on the same state correctly returns 1 row with v=null, "
        "proving the row data is present and only `SELECT *` regresses.) The fix (3.11.11) checks "
        "row.primaryKeyLivenessInfo().isEmpty() before treating the all-columns UPDATE row as "
        "complete, so the read does not stop early and the PK liveness is preserved. Component: "
        "Legacy/Local Write-Read Paths."
    )

    def post_deploy(self):
        """Disable hinted handoff on both replicas so divergence is never erased by hint replay."""
        for pod in ("cass-0", "cass-1"):
            self.app.disablehandoff(pod)

    def _create_schema(self):
        self.app.cqlsh(
            "cass-0",
            f"CREATE KEYSPACE IF NOT EXISTS {_KS} WITH replication = "
            "{'class':'SimpleStrategy','replication_factor':2}; "
            f"CREATE TABLE IF NOT EXISTS {_TABLE} (pk int, ck text, v int, PRIMARY KEY (pk, ck)) "
            "WITH read_repair_chance = 0 AND dclocal_read_repair_chance = 0;",
        )

    def reestablish_divergence(self):
        """(Re)create fresh per-replica divergence on ks16671.tbl via gossip isolation.

        Idempotent and deterministic: a CONSISTENCY ALL read triggers blocking read-repair
        that reconciles the divergence, so the oracle calls this right before each measurement
        to guarantee a clean signal regardless of prior reads. cass-0 ends with TWO sstables
        (INSERT @ts1000 carrying PK liveness + all-columns UPDATE @ts2000 carrying none); cass-1
        ends with a single column-DELETE tombstone @ts3000.
        """
        app = self.app
        c0, c1 = "cass-0", "cass-1"
        ip0, ip1 = app.pod_ip(c0), app.pod_ip(c1)

        # Start from a clean, fully-up ring and an empty table.
        app.enablegossip(c0)
        app.enablegossip(c1)
        app.wait_ring(2)
        app.cqlsh(c0, f"CONSISTENCY ALL; TRUNCATE {_TABLE};")

        # STEP A — isolate cass-1; write cass-0-only data, flushing after each write so cass-0
        # holds two sstables (INSERT sstable WITH PK liveness, all-columns UPDATE sstable WITHOUT).
        app.disablegossip(c1)
        app.wait_node_state(c0, ip1, "DN", timeout=120)
        app.cqlsh(
            c0,
            f"CONSISTENCY ONE; INSERT INTO {_TABLE} (pk, ck, v) VALUES (1, '1', 1) USING TIMESTAMP 1000;",
        )
        app.flush(c0, _KS)
        app.cqlsh(
            c0,
            f"CONSISTENCY ONE; UPDATE {_TABLE} USING TIMESTAMP 2000 SET v = 2 WHERE pk = 1 AND ck = '1';",
        )
        app.flush(c0, _KS)

        # STEP B — re-enable cass-1, isolate cass-0; write the column DELETE on cass-1 only.
        app.enablegossip(c1)
        app.wait_node_state(c0, ip1, "UN", timeout=120)
        app.disablegossip(c0)
        app.wait_node_state(c1, ip0, "DN", timeout=120)
        app.cqlsh(
            c1,
            f"CONSISTENCY ONE; DELETE v FROM {_TABLE} USING TIMESTAMP 3000 WHERE pk = 1 AND ck = '1';",
        )
        app.flush(c1, _KS)

        # STEP C — re-enable cass-0; both nodes back to UN with divergence preserved.
        app.enablegossip(c0)
        app.wait_ring(2)

    @mark_fault_injected
    def inject_fault(self):
        """Create schema + initial per-replica divergence, and log the buggy signature once."""
        self._create_schema()
        self.reestablish_divergence()
        out = self.app.cqlsh("cass-1", f"CONSISTENCY ALL; {_MONEY_QUERY}")
        import logging

        logging.getLogger(__name__).info(f"[16671] inject_fault money-query output:\n{out}")

    def build_mitigation_oracle(self):
        return CassandraWrongResultOracle(
            problem=self,
            query=_MONEY_QUERY,
            pod="cass-1",
            consistency="ALL",
            buggy_regex=r"\(0 rows\)",
            reestablish=True,
        )
