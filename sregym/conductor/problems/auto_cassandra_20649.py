"""CASSANDRA-20649: dotted snapshot tags make Descriptor log keyspace/table extraction failures."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra20649(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "5.0.4"
    cassandra_version = "5.0.4"
    source_git_ref = "cassandra-5.0.4"
    ring_namespace = "cassraw-20649"

    root_cause_file = "src/java/org/apache/cassandra/io/sstable/Descriptor.java"
    root_cause_description = (
        "Snapshot paths whose tag contains dots are parsed as if the dot belonged to an SSTable directory "
        "component. In 5.0.4 Descriptor logs repeated 'Could not extract keyspace/table info from sstable "
        "directory ... snapshots/name.with.dot/...'; 5.0.5 no longer emits that debug signature."
    )
    bug_pattern = r"Could not extract keyspace/table info from sstable directory .*name\.with\.dot"

    def observe_bug(self) -> str:
        self.cql_file(
            """
            CREATE KEYSPACE IF NOT EXISTS repro WITH replication = {'class':'SimpleStrategy','replication_factor':1};
            CREATE TABLE IF NOT EXISTS repro.t (id int PRIMARY KEY, v text);
            INSERT INTO repro.t (id, v) VALUES (1, 'a');
            """,
            timeout=120,
        )
        self.app.flush(self.pod, "repro")
        self.app.nodetool(self.pod, "snapshot -t name.with.dot repro")
        return (
            self.app.nodetool(self.pod, "listsnapshots")
            + "\n"
            + self.sh("grep -R 'Could not extract keyspace/table info' /var/log/cassandra 2>/dev/null || true")
        )
