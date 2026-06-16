"""CASSANDRA-20189: SAI intersection over a repaired index match + multiple non-indexed matches drops a row.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20189
Buggy: 5.0.3  ->  Fixed: 5.0.4 (also 6.0)
Components: Consistency/Coordination, Feature/SAI

Reproduced on a raw 2-node ring (RF=2, read_repair='NONE', hinted handoff disabled), as the
real-ring translation of the in-JVM dtest ``testPartialUpdatesOnNonIndexedColumnsAfterRepair``:
  1. INSERT (k=0, a=1) at CL ALL, flush both nodes, then run an INCREMENTAL ``nodetool repair`` so
     the a=1 sstable is marked repairedAt>0 on BOTH replicas (the "repaired index match").
  2. Split the row across replicas via gossip isolation + CL ONE writes: b=2 to cass-0 only,
     c=3 to cass-1 only; flush both. No single replica holds both b and c.
  3. SELECT * WHERE a=1 AND b=2 AND c=3 ALLOW FILTERING at CL ALL: because the index column
     returned only repaired matches while MULTIPLE non-indexed columns (b, c) still need filtering,
     FilterTree applies strict per-replica post-filtering BEFORE coordinator reconciliation, neither
     replica alone satisfies a AND b AND c, and the reconciled row is silently dropped.

VERBATIM BUGGY SIGNATURE (buggy 5.0.3), both queries at CONSISTENCY ALL:
  SELECT * FROM repro20189.partial_updates WHERE a=1 AND b=2 AND c=3 ALLOW FILTERING;  -> (0 rows)
while the identical-CL primary-key read proves the reconciled row really exists:
  SELECT * FROM repro20189.partial_updates WHERE k=0;
   k | a | b | c
   0 | 1 | 2 | 3
  (1 rows)
The ABSENCE of the row from the SAI intersection result IS the bug. On fixed 5.0.4 the identical SAI
intersection query returns the row (1 rows). No fixed image is needed to grade it: the within-version
control is the PK read (1 row) vs the SAI intersection (0 rows) on the SAME diverged, repaired data.
"""

import logging

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraWrongResultOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KS = "repro20189"
_TABLE = f"{_KS}.partial_updates"
# The SAI intersection (repaired index match + multiple non-indexed predicates). Buggy 5.0.3 drops
# the reconciled row -> (0 rows); a fixed binary returns it -> (1 rows).
_MONEY_QUERY = f"SELECT * FROM {_TABLE} WHERE a = 1 AND b = 2 AND c = 3 ALLOW FILTERING;"


class AutoCassandra20189(CassandraRawRingProblem):
    """SAI-intersection consistency-violation (dropped row) on a raw 2-node ring (RF=2).

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:5.0.3`` ring; ``inject_fault`` creates the SAI schema, marks the a=1 sstable
    repaired, and splits b/c across replicas via gossip isolation; the
    ``CassandraWrongResultOracle`` re-establishes that state and runs the SAI intersection
    query, grading the bug present when it returns ``(0 rows)`` (buggy 5.0.3) vs the row
    ``(1 rows)`` (fixed 5.0.4).
    """

    db_name = "cassandra"
    db_version = "5.0.3"
    cassandra_version = "5.0.3"
    source_git_ref = "cassandra-5.0.3"
    ring_namespace = "cassraw-20189"
    replicas = 2

    root_cause_file = "src/java/org/apache/cassandra/index/sai/plan/FilterTree.java"
    root_cause_description = (
        "An SAI intersection query (ALLOW FILTERING) whose indexed column returns only repaired "
        "matches while MULTIPLE non-indexed columns still need filtering can violate consistency. "
        "FilterTree applies strict per-replica post-filtering: each replica is filtered for "
        "a AND b AND c BEFORE coordinator reconciliation. After an incremental repair marks the "
        "a=1 sstable repairedAt>0 on both replicas and the row is split across replicas (b=2 on one, "
        "c=3 on the other) with read_repair='NONE', neither replica alone satisfies all predicates, "
        "so the row is dropped from the intersection result even though the reconciled row "
        "(k=0,a=1,b=2,c=3) exists and a CL ALL primary-key read returns it. Correct result is the "
        "single row; the buggy 5.0.3 coordinator returns (0 rows). Fixed in 5.0.4 / 6.0."
    )

    def _pods(self):
        return [f"cass-{i}" for i in range(self.replicas)]

    def post_deploy(self):
        """Restore the cassandra tool PATH (5.0.x quirk), then disable handoff + autocompaction.

        The app issues every nodetool/cqlsh via ``bash -lc`` whose login PATH drops
        /opt/cassandra/{bin,tools/bin} on the 5.0.x image; re-add it before any nodetool call.
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
            "{'class':'NetworkTopologyStrategy','dc1':2}; "
            f"CREATE TABLE IF NOT EXISTS {_TABLE} (k int PRIMARY KEY, a int, b int, c int) "
            "WITH read_repair = 'NONE'; "
            f"CREATE INDEX IF NOT EXISTS partial_updates_a_idx ON {_TABLE} (a) USING 'sai';",
        )

    def reestablish_divergence(self):
        """(Re)create the repaired index match + per-replica b/c split on repro20189.partial_updates.

        Idempotent and deterministic: truncates, re-inserts the repaired a=1 row, then re-splits
        b=2/c=3 across replicas via gossip isolation, so the oracle gets a clean signal each call.
        """
        app = self.app
        c0, c1 = "cass-0", "cass-1"
        ip0, ip1 = app.pod_ip(c0), app.pod_ip(c1)

        # Clean, fully-up ring + empty table.
        app.enablegossip(c0)
        app.enablegossip(c1)
        app.wait_ring(self.replicas)
        app.cqlsh(c0, f"CONSISTENCY ALL; TRUNCATE {_TABLE};")

        # STEP 1 — repaired index match: write a=1 to both, flush, incremental repair.
        app.cqlsh(c0, f"CONSISTENCY ALL; INSERT INTO {_TABLE} (k, a) VALUES (0, 1) USING TIMESTAMP 1;")
        app.flush(c0, _KS)
        app.flush(c1, _KS)
        app.nodetool(c0, f"repair {_KS}")

        # STEP 2a — b=2 on cass-0 only (isolate cass-1).
        app.disablegossip(c1)
        app.wait_node_state(c0, ip1, "DN", timeout=180)
        app.cqlsh(c0, f"CONSISTENCY ONE; INSERT INTO {_TABLE} (k, b) VALUES (0, 2) USING TIMESTAMP 2;")
        app.enablegossip(c1)
        app.wait_node_state(c0, ip1, "UN", timeout=120)

        # STEP 2b — c=3 on cass-1 only (isolate cass-0).
        app.disablegossip(c0)
        app.wait_node_state(c1, ip0, "DN", timeout=180)
        app.cqlsh(c1, f"CONSISTENCY ONE; INSERT INTO {_TABLE} (k, c) VALUES (0, 3) USING TIMESTAMP 3;")
        app.enablegossip(c0)
        app.wait_ring(self.replicas)

        # Flush the split so the SAI post-filter reads it from sstables on each replica.
        app.flush(c0, _KS)
        app.flush(c1, _KS)

    @mark_fault_injected
    def inject_fault(self):
        """Create SAI schema + the repaired/split divergence; log both the PK read and the buggy SAI query."""
        self._create_schema()
        self.reestablish_divergence()
        pk = self.app.cqlsh("cass-0", f"CONSISTENCY ALL; SELECT * FROM {_TABLE} WHERE k = 0;")
        sai = self.app.cqlsh("cass-0", f"CONSISTENCY ALL; {_MONEY_QUERY}")
        logger.info(f"[20189] inject_fault PK read (expect 1 row):\n{pk}")
        logger.info(f"[20189] inject_fault SAI intersection (buggy = 0 rows):\n{sai}")

    def build_mitigation_oracle(self):
        return CassandraWrongResultOracle(
            problem=self,
            query=_MONEY_QUERY,
            pod="cass-0",
            consistency="ALL",
            buggy_regex=r"\(0 rows\)",
            reestablish=True,
        )
