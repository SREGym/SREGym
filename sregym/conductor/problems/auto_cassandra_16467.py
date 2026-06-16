"""CASSANDRA-16467: speculative_retry='none' is rejected instead of normalized to NONE."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra16467(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "3.11.10"
    cassandra_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    ring_namespace = "cassraw-16467"

    root_cause_file = "src/java/org/apache/cassandra/schema/SpeculativeRetryParam.java"
    root_cause_description = (
        "The speculative_retry table option should accept the case-insensitive value 'none' and normalize it "
        "to NONE. Cassandra 3.11.10 rejects the value with ConfigurationException 'Invalid value none for "
        "option speculative_retry'; 3.11.11 accepts the same CQL."
    )
    bug_pattern = r"Invalid value none for option 'speculative_retry'"

    def observe_bug(self) -> str:
        return self.cql(
            "DROP KEYSPACE IF EXISTS repro16467; "
            "CREATE KEYSPACE repro16467 WITH replication = {'class':'SimpleStrategy','replication_factor':1}; "
            "CREATE TABLE repro16467.t (k int PRIMARY KEY) WITH speculative_retry = 'none';",
            timeout=120,
        )
