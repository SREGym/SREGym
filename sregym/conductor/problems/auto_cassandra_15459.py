"""CASSANDRA-15459: Short read protection doesn't work on group-by queries.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-15459

Buggy: 3.11.7  ->  Fixed: 3.11.8 (also 4.0-beta2 / 4.0).

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-node ring, RF=2, coordinator-side bug):
  On a two-node cluster with RF=2, per-replica divergence is injected via gossip
  isolation (nodetool disablegossip on the peer) so CONSISTENCY ONE writes land on
  only one replica, with hinted handoff disabled so the divergence is preserved.
  Node1 (cass-0) gets INSERT(pk=1,c=1)@ts9, DELETE(pk=0,c=0)@ts10, INSERT(pk=2,c=2)@ts9;
  Node2 (cass-1) gets DELETE(pk=1,c=1)@ts10, INSERT(pk=0,c=0)@ts9, DELETE(pk=2,c=2)@ts10.
  After gossip is re-enabled (both UN), a coordinator running
  `CONSISTENCY ALL; SELECT pk, c FROM k15459.t GROUP BY pk LIMIT 1` must merge the two
  replicas — every partition's delete@ts10 beats its insert@ts9, so the correct merged
  result is (0 rows). The buggy 3.11.7 coordinator's Short Read Protection recomputes
  the LIMIT using a ROW count instead of a GROUP count, short-circuits early, and
  surfaces a deleted row. (The exact wrong row shifts as blocking read-repair reconciles
  state, e.g. [0,0] on the first run then [2,2] — characteristic of SRP miscounting
  groups as rows, NOT a fixed stale read.)

This bug is coordinator-side merge logic over two divergent replicas; it CANNOT manifest
on a single node and CANNOT be expressed as one single-cluster CQL string. It therefore
requires multi-pod orchestration (per-node gossip isolation + per-node CL ONE writes) that
is not yet available in the single-cluster GenericCustomBuildProblem harness. The full
reproducer steps from the evidence log are preserved in the `reproducer` field below so
this can be promoted to a real multi-node Problem later.

Verbatim buggy signature (cassandra:3.11.7, first run):
  Consistency level set to ALL.

   pk | c
  ----+---
    0 | 0

  (1 rows)

  Warnings :
  Aggregation query used without partition key

Fixed 3.11.8 returns (0 rows) for the identical workload and query.
"""

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraWrongResultOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

# Keyspace/table and the divergence pattern from the evidence log (see module docstring).
_KS = "k15459"
_TABLE = f"{_KS}.t"
_MONEY_QUERY = f"SELECT pk, c FROM {_TABLE} GROUP BY pk LIMIT 1;"


class AutoCassandra15459(CassandraRawRingProblem):
    """SRP-on-GROUP-BY wrong-result bug, reproduced on a raw 2-node ring (RF=2).

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:3.11.7`` ring; ``inject_fault`` creates the schema and per-replica
    divergence via gossip isolation; the ``CassandraWrongResultOracle`` re-establishes
    fresh divergence and runs the coordinator GROUP BY query, grading the bug as present
    when a deleted partition is returned (buggy 3.11.7) vs ``(0 rows)`` (fixed 3.11.8).
    """

    db_name = "cassandra"
    db_version = "3.11.7"
    cassandra_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    ring_namespace = "cassraw-15459"
    replicas = 2

    root_cause_file = "src/java/org/apache/cassandra/service/DataResolver.java"
    root_cause_description = (
        "Coordinator-side Short Read Protection (SRP) does not work for GROUP BY queries. "
        "When per-replica divergence causes a `GROUP BY ... LIMIT` query to short-read, the "
        "SRP path in DataResolver recomputes the remaining limit using a ROW count instead of "
        "a GROUP count, so it stops fetching early and surfaces a partition (e.g. pk=0) that is "
        "actually deleted on every replica once correctly merged (every delete@ts10 beats its "
        "insert@ts9). The correct merged result is (0 rows); the buggy coordinator returns a "
        "deleted row. Component: Legacy/Coordination. Fixed in 3.11.8 / 4.0-beta2 / 4.0."
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
            f"CREATE TABLE IF NOT EXISTS {_TABLE} (pk int, c int, PRIMARY KEY (pk, c)) "
            "WITH read_repair_chance = 0 AND dclocal_read_repair_chance = 0;",
        )

    def reestablish_divergence(self):
        """(Re)create fresh per-replica divergence on k15459.t via gossip isolation.

        Idempotent and deterministic: a CONSISTENCY ALL read triggers blocking read-repair
        that reconciles the divergence, so the oracle calls this right before each
        measurement to guarantee a clean signal regardless of prior reads.
        """
        app = self.app
        c0, c1 = "cass-0", "cass-1"
        ip0, ip1 = app.pod_ip(c0), app.pod_ip(c1)

        # Start from a clean, fully-up ring and an empty table.
        app.enablegossip(c0)
        app.enablegossip(c1)
        app.wait_ring(2)
        app.cqlsh(c0, f"CONSISTENCY ALL; TRUNCATE {_TABLE};")

        # STEP A — isolate cass-1; write NODE1-only data on cass-0 at CONSISTENCY ONE.
        app.disablegossip(c1)
        app.wait_node_state(c0, ip1, "DN", timeout=120)
        app.cqlsh(
            c0,
            f"CONSISTENCY ONE; "
            f"INSERT INTO {_TABLE} (pk, c) VALUES (1, 1) USING TIMESTAMP 9; "
            f"DELETE FROM {_TABLE} USING TIMESTAMP 10 WHERE pk = 0 AND c = 0; "
            f"INSERT INTO {_TABLE} (pk, c) VALUES (2, 2) USING TIMESTAMP 9;",
        )
        app.flush(c0, _KS)

        # STEP B — re-enable cass-1, isolate cass-0; write NODE2-only data on cass-1.
        app.enablegossip(c1)
        app.wait_node_state(c0, ip1, "UN", timeout=120)
        app.disablegossip(c0)
        app.wait_node_state(c1, ip0, "DN", timeout=120)
        app.cqlsh(
            c1,
            f"CONSISTENCY ONE; "
            f"DELETE FROM {_TABLE} USING TIMESTAMP 10 WHERE pk = 1 AND c = 1; "
            f"INSERT INTO {_TABLE} (pk, c) VALUES (0, 0) USING TIMESTAMP 9; "
            f"DELETE FROM {_TABLE} USING TIMESTAMP 10 WHERE pk = 2 AND c = 2;",
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
        out = self.app.cqlsh("cass-0", f"CONSISTENCY ALL; {_MONEY_QUERY}")
        import logging

        logging.getLogger(__name__).info(f"[15459] inject_fault money-query output:\n{out}")

    def build_mitigation_oracle(self):
        return CassandraWrongResultOracle(
            problem=self,
            query=_MONEY_QUERY,
            pod="cass-0",
            consistency="ALL",
            min_buggy_rows=1,
            reestablish=True,
        )
