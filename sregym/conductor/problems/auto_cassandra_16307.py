"""CASSANDRA-16307: GROUP BY queries with paging can return deleted data.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16307

Buggy: 3.11.10  ->  Fixed: 3.11.11 (also 4.0-rc1 / 4.0).

Reproduction summary (2-node ring, RF=2, coordinator-side bug):
  Insert (pk=0,ck=0) and (pk=1,ck=1) at CL=ALL so both partitions live on both replicas,
  then create mirror-image per-replica divergence by deleting (1,1) on cass-1 ONLY and (0,0)
  on cass-0 ONLY. The node-local deletes are emulated on a real ring via gossip isolation
  (the issuing node is the one that currently sees its peer DOWN, so a CONSISTENCY ONE delete
  is not forwarded), with hinted handoff DISABLED so the divergence is not silently healed by
  hint replay. After the ring re-forms (2 UN), the FIRST
  `CONSISTENCY ALL; PAGING 1; SELECT * FROM ks16307.t GROUP BY pk;` (run before blocking
  read-repair heals the divergence) wrongly returns a tombstoned partition. The correct result
  is 0 rows: at CL=ALL both deletes (at the higher timestamp) win on reconciliation. The defect
  is specific to GROUP BY + paging at CL>ONE — non-paged and non-GROUP-BY variants on the SAME
  diverged data both correctly return 0 rows.

This bug is coordinator-side merge logic over two divergent replicas; it CANNOT manifest on a
single node and CANNOT be expressed as one single-cluster CQL string. It is realised here on a
raw 2-node ring: `deploy_app` stands up a stock `cassandra:3.11.10` ring; `inject_fault` creates
the schema and per-replica divergence via gossip isolation; the `CassandraWrongResultOracle`
re-establishes fresh divergence and runs the paged coordinator GROUP BY query, grading the bug
as present when a deleted partition is returned (buggy 3.11.10) vs `(0 rows)` (fixed 3.11.11).

Verbatim buggy signature (cassandra:3.11.10, CL=ALL / PAGING 1 / GROUP BY, first run):

    Consistency level set to ALL.
    Page size: 1

     pk | ck
    ----+----
      0 |  0

    (1 rows)

    Warnings :
    Aggregation query used without partition key

Fixed 3.11.11 returns (0 rows) for the identical workload and query.
"""

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraWrongResultOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

# Keyspace/table and the divergence pattern from the evidence log (see module docstring).
_KS = "ks16307"
_TABLE = f"{_KS}.t"
# Page size 1 is essential: the bug is specific to paged GROUP BY at CL>ONE. The oracle prepends
# `CONSISTENCY ALL; `, so the discriminating statement runs at CL=ALL with PAGING 1.
_MONEY_QUERY = f"PAGING 1; SELECT * FROM {_TABLE} GROUP BY pk;"

# Explicit logical timestamps so the deletes deterministically win on reconciliation: each delete
# (ts 200) is strictly newer than its insert (ts 100), independent of wall-clock timing.
_INSERT_TS = 100
_DELETE_TS = 200


class AutoCassandra16307(CassandraRawRingProblem):
    """Paged-GROUP-BY-returns-deleted-data wrong-result bug, on a raw 2-node ring (RF=2)."""

    db_name = "cassandra"
    db_version = "3.11.10"
    cassandra_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    ring_namespace = "cassraw-16307"
    replicas = 2

    root_cause_file = "src/java/org/apache/cassandra/db/filter/DataLimits.java"
    root_cause_description = (
        "A paged GROUP BY query at CL>ONE/LOCAL_ONE can return a row from a partition that has been "
        "deleted on all replicas. With a 2-node RF=2 cluster, two partitions are inserted on both "
        "nodes and then each is deleted node-locally on a different replica, so each replica sees a "
        "different partition alive but reconciliation must yield zero live partitions. With a page "
        "size of 1, GROUP BY wrongly returns one of the tombstoned partitions. The defect is in the "
        "GROUP BY paging/counting coordination path: DataLimits.GroupByAwareCounter (the "
        "hasGroupStarted state, fixed/renamed to hasUnfinishedGroup) miscounts groups across page "
        "boundaries, and the coordinator short-read protection in DataResolver.java does not correctly "
        "detect the exhausted limit. Non-paged and non-GROUP-BY queries on the same diverged data "
        "reconcile correctly to 0 rows. Component: Consistency/Coordination. Fixed in 3.11.11 / 4.0."
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
            f"CREATE TABLE IF NOT EXISTS {_TABLE} (pk int, ck int, PRIMARY KEY (pk, ck)) "
            "WITH read_repair_chance = 0 AND dclocal_read_repair_chance = 0;",
        )

    def reestablish_divergence(self):
        """(Re)create fresh mirror per-replica divergence on ks16307.t via gossip isolation.

        Idempotent and deterministic: a CONSISTENCY ALL read triggers blocking read-repair that
        reconciles the divergence, so the oracle calls this right before each measurement to
        guarantee a clean signal regardless of prior reads.
        """
        app = self.app
        c0, c1 = "cass-0", "cass-1"
        ip0, ip1 = app.pod_ip(c0), app.pod_ip(c1)

        # Start from a clean, fully-up ring and an empty table.
        app.enablegossip(c0)
        app.enablegossip(c1)
        app.wait_ring(2)
        app.cqlsh(c0, f"CONSISTENCY ALL; TRUNCATE {_TABLE};")

        # Seed BOTH partitions on BOTH replicas at CL=ALL (both rows replicated everywhere).
        app.cqlsh(
            c0,
            f"CONSISTENCY ALL; "
            f"INSERT INTO {_TABLE} (pk, ck) VALUES (0, 0) USING TIMESTAMP {_INSERT_TS}; "
            f"INSERT INTO {_TABLE} (pk, ck) VALUES (1, 1) USING TIMESTAMP {_INSERT_TS};",
        )
        app.flush(c0, _KS)
        app.flush(c1, _KS)

        # STEP A — isolate cass-0; delete (1,1) on cass-1 ONLY at CL ONE.
        # cass-1 keeps gossip on, marks the silent cass-0 DOWN, so it does not forward the delete.
        app.disablegossip(c0)
        app.wait_node_state(c1, ip0, "DN", timeout=120)
        app.cqlsh(
            c1,
            f"CONSISTENCY ONE; DELETE FROM {_TABLE} USING TIMESTAMP {_DELETE_TS} WHERE pk = 1 AND ck = 1;",
        )
        app.flush(c1, _KS)

        # STEP B — re-enable cass-0, isolate cass-1; delete (0,0) on cass-0 ONLY at CL ONE.
        app.enablegossip(c0)
        app.wait_node_state(c1, ip0, "UN", timeout=120)
        app.disablegossip(c1)
        app.wait_node_state(c0, ip1, "DN", timeout=120)
        app.cqlsh(
            c0,
            f"CONSISTENCY ONE; DELETE FROM {_TABLE} USING TIMESTAMP {_DELETE_TS} WHERE pk = 0 AND ck = 0;",
        )
        app.flush(c0, _KS)

        # STEP C — re-enable cass-1; both nodes back to UN with mirror divergence preserved.
        app.enablegossip(c1)
        app.wait_ring(2)

    @mark_fault_injected
    def inject_fault(self):
        """Create schema + initial mirror divergence, and log the buggy signature once."""
        self._create_schema()
        self.reestablish_divergence()
        out = self.app.cqlsh("cass-0", f"CONSISTENCY ALL; {_MONEY_QUERY}")
        import logging

        logging.getLogger(__name__).info(f"[16307] inject_fault paged-group-by output:\n{out}")

    def build_mitigation_oracle(self):
        return CassandraWrongResultOracle(
            problem=self,
            query=_MONEY_QUERY,
            pod="cass-0",
            consistency="ALL",
            min_buggy_rows=1,
            reestablish=True,
        )
