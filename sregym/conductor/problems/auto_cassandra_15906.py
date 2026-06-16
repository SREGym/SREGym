"""CASSANDRA-15906: KEYS 2i queries break after DROP COMPACT STORAGE."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra15906(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "3.0.20"
    cassandra_version = "3.0.20"
    source_git_ref = "cassandra-3.0.20"
    image_override = "mirror.gcr.io/library/cassandra:3.0.20"
    ring_namespace = "cassraw-15906"

    root_cause_file = "src/java/org/apache/cassandra/cql3/statements/SelectStatement.java"
    root_cause_description = (
        "After DROP COMPACT STORAGE, declared columns from the former compact thrift table remain static "
        "internally. The 2i validation path no longer applies the compact-table exception and rejects an "
        "indexed query with 'Queries using 2ndary indexes do not support selecting only static columns', "
        "even though the same query worked before DROP COMPACT STORAGE and is accepted by 3.0.21."
    )
    bug_pattern = r"Queries using 2ndary indexes don't support selecting only static columns"

    def observe_bug(self) -> str:
        return self.cql_file(
            """
            DROP KEYSPACE IF EXISTS repro15906d;
            CREATE KEYSPACE repro15906d WITH replication = {'class':'SimpleStrategy','replication_factor':1};
            USE repro15906d;
            CREATE TABLE t (k text PRIMARY KEY, v text) WITH COMPACT STORAGE;
            CREATE INDEX idx_v ON t(v);
            INSERT INTO t (k,v) VALUES ('a','x');
            SELECT v FROM t WHERE v='x';
            ALTER TABLE t DROP COMPACT STORAGE;
            SELECT v FROM t WHERE v='x';
            """,
            timeout=180,
        )
