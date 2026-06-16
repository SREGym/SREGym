"""CASSANDRA-16712: prepared SELECT * stays stale after DROP COMPACT STORAGE."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra16712(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "3.11.10"
    cassandra_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    ring_namespace = "cassraw-16712"

    root_cause_file = "src/java/org/apache/cassandra/schema/SchemaKeyspace.java"
    root_cause_description = (
        "After ALTER TABLE DROP COMPACT STORAGE, prepared SELECT * metadata must be invalidated so the "
        "post-drop synthetic value column appears. Cassandra 3.11.10 keeps the old prepared metadata and "
        "returns only ['pk', 'ck']; 3.11.11 returns ['pk', 'ck', 'value']."
    )
    bug_pattern = r"OLD_PREPARED_AFTER_DROP_COLUMNS \['pk', 'ck'\]"

    def observe_bug(self) -> str:
        script = r"""
set -e
export PYTHONPATH="/opt/cassandra/lib/futures-2.1.6-py2.py3-none-any.zip:/opt/cassandra/lib/six-1.7.3-py2.py3-none-any.zip:/opt/cassandra/lib/cassandra-driver-internal-only-3.10.zip/cassandra-driver-3.10:/opt/cassandra/lib/cassandra-driver-internal-only-3.11.0-bb96859b.zip/cassandra-driver-3.11.0-bb96859b:$PYTHONPATH"
python - <<'PY'
from __future__ import print_function

from cassandra.cluster import Cluster

session = Cluster(["127.0.0.1"]).connect()
session.execute("DROP KEYSPACE IF EXISTS repro16712")
session.execute("CREATE KEYSPACE repro16712 WITH replication = {'class':'SimpleStrategy','replication_factor':1}")
session.set_keyspace("repro16712")
session.execute("CREATE TABLE t (pk int, ck int, PRIMARY KEY (pk, ck)) WITH COMPACT STORAGE")
session.execute("INSERT INTO t (pk, ck) VALUES (1, 51)")
prepared = session.prepare("SELECT * FROM t WHERE pk = ? AND ck = ?")
before = next(iter(session.execute(prepared, (1, 51))))
print("BEFORE_DROP_COLUMNS %s %s" % (list(before._fields), tuple(before)))
session.execute("ALTER TABLE t DROP COMPACT STORAGE")
old = next(iter(session.execute(prepared, (1, 51))))
print("OLD_PREPARED_AFTER_DROP_COLUMNS %s %s" % (list(old._fields), tuple(old)))
reprepared = session.prepare("SELECT * FROM t WHERE pk = ? AND ck = ?")
new = next(iter(session.execute(reprepared, (1, 51))))
print("REPREPARED_AFTER_DROP_COLUMNS %s %s" % (list(new._fields), tuple(new)))
PY
"""
        return self.sh(script, timeout=240)
