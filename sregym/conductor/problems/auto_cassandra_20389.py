"""CASSANDRA-20389: CREATE TABLE with a long table name kills native transport."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra20389(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "5.0.5"
    cassandra_version = "5.0.5"
    source_git_ref = "cassandra-5.0.5"
    ring_namespace = "cassraw-20389"

    root_cause_file = "src/java/org/apache/cassandra/db/Directories.java"
    root_cause_description = (
        "Very long table names should be rejected before schema creation. In 5.0.5 Cassandra attempts to "
        "create the table directory, hits the filesystem filename limit, logs Directories.java:279 'Failed "
        "to create ... directory', and stops native transport; 5.0.6 rejects the identifier cleanly."
    )
    bug_pattern = r"Failed to create .* directory|File name too long|Stopping transports as disk_failure_policy is stop"

    def observe_bug(self) -> str:
        logs = self.app.system_log(self.pod) + "\n" + self.app.pod_logs_all(self.pod)
        if "Failed to create" in logs:
            return logs
        table = "test_create_" + "z" * 230 + "aaaaaaaaaaaaaaaa"
        cql = f"""
        DROP KEYSPACE IF EXISTS "38373639353166362d3566313";
        CREATE KEYSPACE "38373639353166362d3566313"
            WITH replication = {{'class':'SimpleStrategy', 'replication_factor':1}};
        CREATE TABLE "38373639353166362d3566313".{table} (key int PRIMARY KEY, val int);
        """
        out = self.cql_file(cql, timeout=180)
        return out + "\n" + self.app.system_log(self.pod) + "\n" + self.app.pod_logs_all(self.pod)
