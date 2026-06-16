"""CASSANDRA-19166: many schema changes produce StackOverflowError in TableMetadataRefCache."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra19166(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "4.1.3"
    cassandra_version = "4.1.3"
    source_git_ref = "cassandra-4.1.3"
    ring_namespace = "cassraw-19166"

    root_cause_file = "src/java/org/apache/cassandra/schema/TableMetadataRefCache.java"
    root_cause_description = (
        "TableMetadataRefCache wraps its map fields in Collections.unmodifiableMap on every local schema "
        "update. Repeated ALTER TABLE operations build a deeply nested chain of wrappers; after thousands "
        "of schema changes, a lookup overflows the stack. Cassandra 4.1.3 reports StackOverflowError, while "
        "4.1.4 completes the same ALTER loop."
    )
    bug_pattern = r"StackOverflowError|ALTER_EXCEPTION_AT"

    def observe_bug(self) -> str:
        script = r"""
set -e
CQLSH=$(command -v cqlsh || true)
[ -n "$CQLSH" ] || CQLSH=/opt/cassandra/bin/cqlsh
"$CQLSH" --request-timeout=90 -e "DROP KEYSPACE IF EXISTS repro19166; CREATE KEYSPACE repro19166 WITH replication = {'class':'SimpleStrategy','replication_factor':1}; CREATE TABLE repro19166.t (k int PRIMARY KEY, v int);"
for start in $(seq 1 100 8000); do
  end=$((start + 99))
  : >/tmp/alters.cql
  for i in $(seq "$start" "$end"); do
    echo "ALTER TABLE repro19166.t WITH comment = 'alter_$i';" >>/tmp/alters.cql
  done
  set +e
  out=$("$CQLSH" --request-timeout=90 -f /tmp/alters.cql 2>&1)
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "ALTER_EXCEPTION_AT $start"
    echo "$out"
    exit 0
  fi
  if [ "$end" -ge 8000 ]; then
    echo "NO_EXCEPTION_AFTER 8000"
  fi
done
"""
        return self.sh(script, timeout=900)
