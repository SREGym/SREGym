"""CASSANDRA-20365: DESCRIBE TABLE omits materialized views."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra20365(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "5.0.3"
    cassandra_version = "5.0.3"
    source_git_ref = "cassandra-5.0.3"
    ring_namespace = "cassraw-20365"

    root_cause_file = "src/java/org/apache/cassandra/cql3/statements/schema/DescribeStatement.java"
    root_cause_description = (
        "Server-side DESCRIBE TABLE should include materialized views attached to the base table. In 5.0.3 "
        "DESCRIBE TABLE repro20365.base prints only CREATE TABLE and omits CREATE MATERIALIZED VIEW; 5.0.4 "
        "includes the view definition."
    )
    bug_pattern = r"BUG_PRESENT: materialized view omitted"

    def observe_bug(self) -> str:
        self.cql_file(
            """
            DROP KEYSPACE IF EXISTS repro20365;
            CREATE KEYSPACE repro20365 WITH replication = {'class':'SimpleStrategy', 'replication_factor':1};
            CREATE TABLE repro20365.base (id int PRIMARY KEY, v int);
            CREATE MATERIALIZED VIEW repro20365.base_by_v AS
                SELECT v, id FROM repro20365.base
                WHERE v IS NOT NULL AND id IS NOT NULL
                PRIMARY KEY (v, id);
            """,
            timeout=120,
        )
        out = self.cql("DESCRIBE TABLE repro20365.base;", timeout=120)
        marker = "BUG_PRESENT: materialized view omitted\n" if "CREATE MATERIALIZED VIEW" not in out else ""
        return marker + out
