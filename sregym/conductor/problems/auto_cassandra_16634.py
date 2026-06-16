"""CASSANDRA-16634: nodetool garbagecollect rewrites LCS SSTables to L0."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra16634(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "3.11.10"
    cassandra_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    ring_namespace = "cassraw-16634"

    root_cause_file = "src/java/org/apache/cassandra/db/compaction/CompactionTask.java"
    root_cause_description = (
        "Garbagecollect compaction on a LeveledCompactionStrategy table should preserve the original SSTable "
        "level. In 3.11.10 the garbagecollect writer emits the replacement SSTable at level 0, causing LCS "
        "tables to collapse into L0 after a garbagecollect operation."
    )
    bug_pattern = r"AFTER_GC.*SSTable Level:\s+0"

    def observe_bug(self) -> str:
        script = r"""
set -e
cat >/tmp/setup16634.cql <<'CQL'
DROP KEYSPACE IF EXISTS repro16634;
CREATE KEYSPACE repro16634 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro16634.t (k int PRIMARY KEY, v text)
  WITH compaction = {'class':'LeveledCompactionStrategy', 'sstable_size_in_mb':'1'};
CQL
for i in $(seq 1 2000); do
  echo "INSERT INTO repro16634.t (k, v) VALUES ($i, 'v$i');" >>/tmp/setup16634.cql
done
CQLSH=$(command -v cqlsh || true)
[ -n "$CQLSH" ] || CQLSH=/opt/cassandra/bin/cqlsh
"$CQLSH" --request-timeout=120 -f /tmp/setup16634.cql
nodetool disableautocompaction repro16634 t
nodetool flush repro16634 t
nodetool compact repro16634 t
TOOL=/opt/cassandra/tools/bin/sstablemetadata
[ -x "$TOOL" ] || TOOL=sstablemetadata
echo AFTER_COMPACT
DATA=$(ls -t /var/lib/cassandra/data/repro16634/t-*/*-Data.db | head -n1)
echo "$DATA"
"$TOOL" "$DATA" | grep 'SSTable Level'
echo RUN_GC
nodetool garbagecollect --granularity ROW repro16634 t
echo AFTER_GC
DATA=$(ls -t /var/lib/cassandra/data/repro16634/t-*/*-Data.db | head -n1)
echo "$DATA"
"$TOOL" "$DATA" | grep 'SSTable Level'
"""
        return self.sh(script, timeout=300)
