"""CASSANDRA-17628: writetime()/ttl() wrongly reject frozen collection columns."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra17628(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "4.0.4"
    cassandra_version = "4.0.4"
    source_git_ref = "cassandra-4.0.4"
    ring_namespace = "cassraw-17628"

    root_cause_file = "src/java/org/apache/cassandra/cql3/selection/Selectable.java"
    root_cause_description = (
        "The selection validation for writetime() and ttl() treats frozen collections like multi-cell "
        "collections. On 4.0.4, writetime(fs) and ttl(fs) on a frozen<set<int>> column are rejected with "
        "'Cannot use selection function ... on collections'; 4.0.5 accepts the query."
    )
    bug_pattern = r"Cannot use selection function (writeTime|ttl) on collections"

    def observe_bug(self) -> str:
        return self.cql_file(
            """
            DROP KEYSPACE IF EXISTS repro17628;
            CREATE KEYSPACE repro17628 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
            CREATE TABLE repro17628.t (id int PRIMARY KEY, fs frozen<set<int>>);
            INSERT INTO repro17628.t (id, fs) VALUES (1, {1, 2});
            SELECT writetime(fs) FROM repro17628.t WHERE id = 1;
            SELECT ttl(fs) FROM repro17628.t WHERE id = 1;
            """,
            timeout=120,
        )
