"""CASSANDRA-20100: SAI range query misses rows for reversed bigint clustering keys."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra20100(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "5.0.2"
    cassandra_version = "5.0.2"
    source_git_ref = "cassandra-5.0.2"
    ring_namespace = "cassraw-20100"

    root_cause_file = "src/java/org/apache/cassandra/index/sai/utils/IndexTermType.java"
    root_cause_description = (
        "SAI indexes special numeric types using custom byte-comparable encodings. When a bigint clustering "
        "key is reversed, 5.0.2 indexes the terms as if they were reversed comparable bytes, breaking range "
        "query construction/post-filtering. A range on c combined with an SAI abbreviation predicate returns "
        "zero rows on 5.0.2 but returns c=3 and c=2 on 5.0.3."
    )
    bug_pattern = r"\(0 rows\)"

    def observe_bug(self) -> str:
        return self.cql_file(
            """
            DROP KEYSPACE IF EXISTS ks20100;
            CREATE KEYSPACE ks20100 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
            CREATE TABLE ks20100.t (
                p int,
                c bigint,
                abbreviation text,
                PRIMARY KEY (p, c)
            ) WITH CLUSTERING ORDER BY (c DESC);
            CREATE CUSTOM INDEX t_abbreviation_idx ON ks20100.t (abbreviation) USING 'StorageAttachedIndex';
            CREATE CUSTOM INDEX t_c_idx ON ks20100.t (c) USING 'StorageAttachedIndex';
            INSERT INTO ks20100.t (p, c, abbreviation) VALUES (0, 1, 'MA');
            INSERT INTO ks20100.t (p, c, abbreviation) VALUES (0, 2, 'MA');
            INSERT INTO ks20100.t (p, c, abbreviation) VALUES (0, 3, 'MA');
            INSERT INTO ks20100.t (p, c, abbreviation) VALUES (0, 4, 'NY');
            SELECT p, c, abbreviation FROM ks20100.t
                WHERE c >= 2 AND c <= 3 AND abbreviation = 'MA';
            """,
            timeout=180,
        )
