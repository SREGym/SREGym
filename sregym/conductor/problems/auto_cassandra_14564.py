"""CASSANDRA-14564: ALTER ADD on COMPACT STORAGE tables without clustering crashes instead of rejecting."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra14564(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "4.0.1"
    cassandra_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    ring_namespace = "cassraw-14564"

    root_cause_file = "src/java/org/apache/cassandra/schema/TableMetadata.java"
    root_cause_description = (
        "Adding a regular column to a COMPACT STORAGE table with no clustering columns should be rejected. "
        "In 4.0.1 the schema mutation is accepted far enough that CompactTableMetadata later cannot resolve "
        "the compact value column, causing an AssertionError in getCompactValueColumn instead of the fixed "
        "InvalidRequestException."
    )
    bug_pattern = r"getCompactValueColumn|AssertionError"

    def observe_bug(self) -> str:
        logs = self.app.system_log(self.pod)
        if "getCompactValueColumn" in logs:
            return logs
        out = self.cql_file(
            """
            DROP KEYSPACE IF EXISTS repro14564;
            CREATE KEYSPACE repro14564 WITH replication = {'class':'SimpleStrategy', 'replication_factor':1};
            CREATE TABLE repro14564.employee (
                emp_id int PRIMARY KEY,
                emp_name text,
                emp_city text,
                emp_sal varint,
                emp_phone varint
            ) WITH COMPACT STORAGE;
            ALTER TABLE repro14564.employee ADD profile text;
            INSERT INTO repro14564.employee (emp_id, emp_name, emp_city, emp_sal, emp_phone, profile)
                VALUES (1, 'a', 'b', 1, 2, 'p');
            SELECT profile FROM repro14564.employee;
            """,
            timeout=180,
        )
        return out + "\n" + self.app.system_log(self.pod)
