"""CASSANDRA-21332: Static SAI queries resurrect range-tombstoned data during Replica Filtering Protection.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21332
Buggy: 5.0.8  ->  Fixed: 5.0.9 (also 6.0-alpha2, 7.x)

Reproduced on a raw 3-node ring (RF=3) via gossip isolation. A static SAI-indexed column with
read_repair='NONE' is queried through the coordinator at CONSISTENCY ALL. Each replica holds
DIFFERENT data for the SAME partition key (pk0=1) — an invariant the normal coordinator/CQL write
path CANNOT produce, so it is staged by isolating each node via `nodetool disablegossip` on the
others and writing at CONSISTENCY ONE:
  - node3 (cass-2): stale row ck0=false @TS1
  - node1 (cass-0): stale row ck0=true  @TS1
  - node2 (cass-1): range tombstone @TS2 covering ck0<=true  +  the only surviving row (s1=42) @TS3
The SAI first-pass query (s1 = 42) matches only node2's surviving row; the Replica Filtering
Protection (RFP) completion reads on node1/node3 then re-read the whole partition WITHOUT being
supplied node2's range tombstone, so the logically-deleted stale rows are NOT shadowed and are
resurrected.

Verbatim buggy signature (coordinator = cass-0, CONSISTENCY ALL):
  SELECT ck0, ck1 FROM rfp21332.rt_static_sai WHERE s1 = 42;
  returns 3 rows  [(False,1),(True,4),(True,5)]  instead of the single correct row (True,5) —
  range-tombstoned static-SAI rows resurrected during Replica Filtering Protection.

Per-node sstabledump confirms the physical divergence that produces it:
  cass-0 -> clustering [true, 4.0]   (stale)
  cass-1 -> range tombstone (marked_deleted @TS2) + clustering [true, 5.0]  (survivor)
  cass-2 -> clustering [false, 1.0]  (stale)

Within-version control (no fixed cassandra:5.0.9 image exists — public 5.0.x ceiling is 5.0.8):
the normal full-partition read on the SAME diverged data correctly returns 1 row (1,True,5,42,True),
proving only the SAI/RFP path is wrong. The oracle distinguishes buggy (>=2 rows) from a fixed
binary (1 row) on the s1=42 SAI query.
"""

import logging

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraWrongResultOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KS = "rfp21332"
_TABLE = f"{_KS}.rt_static_sai"
# The SAI + Replica Filtering Protection money query. Buggy 5.0.8 resurrects the range-tombstoned
# rows (3 rows); a fixed binary returns the single surviving row (1 row).
_MONEY_QUERY = f"SELECT ck0, ck1 FROM {_TABLE} WHERE s1 = 42;"


class AutoCassandra21332(CassandraRawRingProblem):
    """Static-SAI / RFP tombstone-resurrection wrong-result bug on a raw 3-node ring (RF=3).

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:5.0.8`` ring; ``inject_fault`` creates the schema + SAI index and the
    per-replica divergence via gossip isolation; the ``CassandraWrongResultOracle``
    re-establishes fresh divergence and runs the coordinator SAI query, grading the bug as
    present when the range-tombstoned rows are resurrected (>=2 rows, buggy 5.0.8) vs the
    single correct row (1 row, a fixed binary).
    """

    db_name = "cassandra"
    db_version = "5.0.8"
    cassandra_version = "5.0.8"
    source_git_ref = "cassandra-5.0.8"
    ring_namespace = "cassraw-21332"
    replicas = 3

    root_cause_file = "src/java/org/apache/cassandra/service/reads/ReplicaFilteringProtection.java"
    root_cause_description = (
        "Queries on a static StorageAttachedIndex (SAI) column with read_repair='NONE' resurrect "
        "range-tombstoned data during Replica Filtering Protection (RFP). The SAI first-pass query "
        "(s1 = 42) matches only the surviving row on the replica that holds the range tombstone; the "
        "RFP completion reads issued against the other replicas re-read the whole partition WITHOUT "
        "being supplied that range tombstone (which lives only on the tombstone-holding replica), so "
        "the logically-deleted stale rows on those replicas are not shadowed and are returned to the "
        "client. Correct result is the single surviving row; the buggy 5.0.8 coordinator returns the "
        "resurrected rows too. Fixed in 5.0.9 / 6.0-alpha2 / 7.x."
    )

    def _pods(self):
        return [f"cass-{i}" for i in range(self.replicas)]

    def post_deploy(self):
        """Restore the cassandra tool PATH (5.0.x quirk), then disable handoff + autocompaction.

        The app issues every nodetool/cqlsh via ``bash -lc`` whose login PATH drops
        /opt/cassandra/{bin,tools/bin} on the 5.0.x image; re-add it before any nodetool call.
        Hinted handoff + autocompaction are disabled so the per-replica divergence is never
        erased by hint replay or background compaction.
        """
        for pod in self._pods():
            self.app.exec(
                pod,
                "echo 'export PATH=/opt/cassandra/bin:/opt/cassandra/tools/bin:$PATH' > /etc/profile.d/cass.sh",
            )
            self.app.disablehandoff(pod)
            self.app.nodetool(pod, "disableautocompaction")

    def _create_schema(self):
        self.app.cqlsh(
            "cass-0",
            f"CREATE KEYSPACE IF NOT EXISTS {_KS} WITH replication = "
            "{'class':'NetworkTopologyStrategy','dc1':3}; "
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "pk0 int, ck0 boolean, ck1 double, s1 int static, v0 boolean, "
            "PRIMARY KEY (pk0, ck0, ck1)) WITH read_repair = 'NONE'; "
            f"CREATE CUSTOM INDEX IF NOT EXISTS ON {_TABLE} (s1) USING 'StorageAttachedIndex';",
        )

    def _isolated_write(self, writer: str, others: list[str], cql: str):
        """Land ``cql`` on ``writer`` only: isolate ``others`` via gossip, write at CL ONE, flush, reconverge."""
        app = self.app
        for o in others:
            app.disablegossip(o)
        for o in others:
            app.wait_node_state(writer, app.pod_ip(o), "DN", timeout=180)
        app.cqlsh(writer, f"CONSISTENCY ONE; {cql}")
        app.flush(writer, _KS)
        for o in others:
            app.enablegossip(o)
        app.wait_ring(self.replicas)

    def reestablish_divergence(self):
        """(Re)create fresh per-replica divergence on rfp21332.rt_static_sai across all 3 nodes.

        Idempotent and deterministic: a CONSISTENCY ALL read can trigger blocking read-repair that
        reconciles the divergence, so the oracle calls this right before each measurement to
        guarantee a clean signal regardless of prior reads.
        """
        app = self.app
        c0, c1, c2 = "cass-0", "cass-1", "cass-2"

        # Start from a clean, fully-up ring and an empty table.
        for pod in (c0, c1, c2):
            app.enablegossip(pod)
        app.wait_ring(self.replicas)
        app.cqlsh(c0, f"CONSISTENCY ALL; TRUNCATE {_TABLE};")

        # Round A — only cass-2 (node3): stale row ck0=false @TS1.
        self._isolated_write(
            c2,
            [c0, c1],
            f"INSERT INTO {_TABLE} (pk0, ck0, ck1, s1, v0) VALUES (1, false, 1.0, 99, false) USING TIMESTAMP 1;",
        )
        # Round B — only cass-0 (node1): stale row ck0=true @TS1.
        self._isolated_write(
            c0,
            [c1, c2],
            f"INSERT INTO {_TABLE} (pk0, ck0, ck1, s1, v0) VALUES (1, true, 4.0, 99, false) USING TIMESTAMP 1;",
        )
        # Round C+D — only cass-1 (node2): range tombstone @TS2 covering ck0<=true, then the
        # single surviving row s1=42 @TS3.
        self._isolated_write(
            c1,
            [c0, c2],
            f"DELETE FROM {_TABLE} USING TIMESTAMP 2 WHERE pk0 = 1 AND ck0 <= true; "
            f"INSERT INTO {_TABLE} (pk0, ck0, ck1, s1, v0) VALUES (1, true, 5.0, 42, true) USING TIMESTAMP 3;",
        )

    @mark_fault_injected
    def inject_fault(self):
        """Create schema + SAI index and the initial per-replica divergence; log the buggy signature once."""
        self._create_schema()
        self.reestablish_divergence()
        out = self.app.cqlsh("cass-0", f"CONSISTENCY ALL; {_MONEY_QUERY}")
        logger.info(f"[21332] inject_fault money-query output:\n{out}")

    def build_mitigation_oracle(self):
        return CassandraWrongResultOracle(
            problem=self,
            query=_MONEY_QUERY,
            pod="cass-0",
            consistency="ALL",
            min_buggy_rows=2,
            reestablish=True,
        )
