"""CASSANDRA-20449: serialization drops a complex deletion in a mutation with multiple collections.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20449
Buggy: 5.0.3  ->  Fixed: 5.0.4 (also 6.0-alpha1, 6.0)
Components: Legacy/Local Write-Read Paths, Local/Commit Log
Fix commit: 1d47fab638e16e103cbeb19fe979806c16b26b45 (PRs #3987, #3992)

Reproduced on a raw 2-node ring (RF=2 via NetworkTopologyStrategy dc1:2 so BOTH nodes are replicas,
read_repair='NONE', hinted handoff disabled, autocompaction disabled). A single row in a table with
THREE collection columns is INSERTed (s1={1}, s2={1}, s3={1}) and then mutated by one UPDATE that
REPLACES one set (SET s2 = {2}, which emits a collection-level complex deletion) while APPENDing to
the others (s1 = s1 + {2}, s3 = s3 + {2}). The coordinator (cass-0) applies the mutation correctly
from its in-memory object, but the SERIALIZED copy delivered to the peer (cass-1) DROPS the s2 complex
deletion, so the peer keeps the older INSERT-time collection tombstone and merges the stale element
{1} into the replacement -> the peer persists s2 = {1, 2} instead of {2}. read_repair='NONE' keeps the
divergence from being healed in the background.

The bug lives ONLY on the peer that received the serialized mutation; a coordinator read at CL > ONE
reconciles and heals it via the UPDATE's higher-timestamp tombstone, and a CL ONE read is routed
non-deterministically. The authoritative, deterministic signature is therefore a per-node sstabledump
of the peer's local Data.db: the buggy peer's s2 column has THREE cells (the complex deletion_info +
path["1"] + path["2"]), whereas a correct replica has only TWO (deletion_info + path["2"]). Because the
workload is always driven through the coordinator cass-0 (which applies locally and correctly), the
buggy replica is deterministically the peer cass-1.

VERBATIM BUGGY SIGNATURE (buggy 5.0.3) — cass-1 (peer) sstabledump s2 cells after `nodetool flush`:
  { "name" : "s2", "deletion_info" : { "marked_deleted" : "<INSERT-time ts>", ... } },
  { "name" : "s2", "path" : [ "1" ], "value" : "" },
  { "name" : "s2", "path" : [ "2" ], "value" : "", "tstamp" : "<UPDATE-time ts>" }
i.e. THREE `"name" : "s2"` cells (the stale element [1] SURVIVED alongside [2] => s2 = {1, 2}). The
discriminator counts these cells: COUNT(name=s2) >= 3 on the buggy peer. On fixed 5.0.4 the peer's
sstabledump shows only TWO s2 cells (deletion_info + path["2"], s2 = {2}); the complex deletion carries
the UPDATE's timestamp and the stale element [1] is gone.
"""

import logging

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KS = "ks20449"
_TABLE = f"{_KS}.multi_collection"

# Discriminator (run on the buggy peer cass-1): flush, dump the newest live Data.db, count the s2
# cells. A correct replica has 2 (deletion_info + path["2"]); the buggy peer has >= 3 (deletion_info +
# path["1"] + path["2"]) because the dropped complex deletion left the stale element [1] in place.
_DISCRIMINATOR_CMD = (
    f"nodetool flush {_KS} multi_collection 2>/dev/null; "
    f"F=$(ls -t /var/lib/cassandra/data/{_KS}/multi_collection-*/*-Data.db 2>/dev/null | head -1); "
    'N=$(sstabledump "$F" 2>/dev/null | grep -cE \'"name"[[:space:]]*:[[:space:]]*"s2"\'); '
    'if [ "$N" -ge 3 ]; then echo "BUG_S2_DIVERGED_PRESENT n=$N file=$F"; '
    'else echo "S2_OK n=$N file=$F"; fi'
)


class AutoCassandra20449(CassandraRawRingProblem):
    """Multi-collection complex-deletion serialization loss on a raw 2-node ring (RF=2).

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:5.0.3`` ring; ``inject_fault`` creates the schema and drives the
    INSERT + multi-collection UPDATE through the coordinator cass-0, diverging the peer cass-1;
    the ``CassandraLogGrepOracle`` re-triggers that workload and grades the bug present when
    cass-1's per-node sstabledump shows >= 3 ``"name" : "s2"`` cells (s2 = {1, 2}, buggy 5.0.3)
    versus exactly 2 (s2 = {2}, fixed 5.0.4).
    """

    db_name = "cassandra"
    db_version = "5.0.3"
    cassandra_version = "5.0.3"
    source_git_ref = "cassandra-5.0.3"
    ring_namespace = "cassraw-20449"
    replicas = 2
    hinted_handoff_enabled = False

    root_cause_file = "src/java/org/apache/cassandra/db/rows/BTreeRow.java"
    root_cause_description = (
        "Serialization can lose complex deletions in a mutation that touches multiple collection "
        "(complex) columns in one row. When a Row is serialized for inter-node delivery, "
        "UnfilteredSerializer first asks BTreeRow.hasComplexDeletion() whether to emit the "
        "complex-deletion (collection tombstone) for each complex column. In the buggy 5.0.3 code that "
        "method walks the row's complex columns with a BTree `accumulate` whose accumulator "
        "(`(cd, v) -> complexDeletion().isLive() ? 0 : Cell.MAX_DELETION_TIME`) IGNORES its running "
        "value `v` and returns Cell.MAX_DELETION_TIME only when THAT column has a complex deletion, "
        "with no early stop. So the final result is whatever the LAST-evaluated complex column returns, "
        "not an OR across all of them. With several collection columns where one is a REPLACEMENT "
        "(SET s2={2}, which carries a complex deletion) and a later one is an APPEND (s3=s3+{2}, no "
        "complex deletion), s2 yields MAX but s3 — evaluated after it in column order (s1,s2,s3) — "
        "returns 0 and overwrites that, so hasComplexDeletion() wrongly returns false and the s2 "
        "complex deletion is omitted from the serialized mutation sent to the peer. The coordinator "
        "applies the mutation correctly from its in-memory object, but the peer that receives the "
        "serialized copy keeps the older INSERT-time collection tombstone and merges the stale element "
        "{1} into the replacement, so it persists s2={1,2} instead of {2}. The fix (5.0.4, commit "
        "1d47fab638e16e103cbeb19fe979806c16b26b45) corrects the hasComplexDeletion() accumulation "
        "(renaming the sentinel to STOP_SENTINEL_VALUE and adding the early stop the buggy code "
        "lacked) so the presence of ANY complex column's deletion is preserved and serialized."
    )

    def _pods(self):
        return [f"cass-{i}" for i in range(self.replicas)]

    def post_deploy(self):
        """Restore the cassandra tool PATH (5.0.x quirk), then disable handoff + autocompaction.

        The app issues every nodetool/cqlsh via ``bash -lc`` whose login PATH drops
        /opt/cassandra/{bin,tools/bin} on the 5.0.x image; re-add it before any nodetool call.
        Disabling autocompaction keeps the per-node sstable the discriminator dumps from being
        merged away.
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
            f"CREATE TABLE IF NOT EXISTS {_TABLE} "
            "(k int, c int, s1 set<int>, s2 set<int>, s3 set<int>, PRIMARY KEY (k, c)) "
            "WITH read_repair = 'NONE';",
        )

    def _run_workload(self):
        """Drive the INSERT + multi-collection UPDATE through cass-0 and flush both replicas.

        Idempotent: truncates first so the oracle gets a clean per-node sstable each call. The
        UPDATE replaces s2 (complex deletion) while appending to s1/s3; on buggy 5.0.3 the serialized
        copy delivered to cass-1 drops the s2 complex deletion, leaving s2 = {1, 2} on the peer.
        """
        app = self.app
        app.enablegossip("cass-0")
        app.enablegossip("cass-1")
        app.wait_ring(self.replicas)
        app.cqlsh("cass-0", f"CONSISTENCY ALL; TRUNCATE {_TABLE};")
        app.cqlsh(
            "cass-0",
            f"CONSISTENCY ALL; "
            f"INSERT INTO {_TABLE} (k, c, s1, s2, s3) VALUES (0, 0, {{1}}, {{1}}, {{1}}); "
            f"UPDATE {_TABLE} SET s2 = {{2}}, s1 = s1 + {{2}}, s3 = s3 + {{2}} WHERE k = 0 AND c = 0;",
        )
        app.flush("cass-0", _KS)
        app.flush("cass-1", _KS)

    def retrigger(self):
        """Re-drive the workload so the oracle measures a fresh, deterministic divergence."""
        self._run_workload()

    @mark_fault_injected
    def inject_fault(self):
        """Create the schema, drive the diverging workload, and log the per-node discriminator."""
        self._create_schema()
        self._run_workload()
        for pod in self._pods():
            out = self.app.exec(pod, _DISCRIMINATOR_CMD)
            logger.info(f"[20449] inject_fault discriminator on {pod} (cass-1 buggy => >=3):\n{out}")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod="cass-1",
            source="command",
            command=_DISCRIMINATOR_CMD,
            pattern=r"BUG_S2_DIVERGED_PRESENT",
            retrigger=True,
            attempts=3,
            retry_delay=5.0,
        )
