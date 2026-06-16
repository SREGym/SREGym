"""CASSANDRA-19537: UTF-8 warning serialization corrupts protocol results for U+10FFFF."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra19537(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "5.0.2"
    cassandra_version = "5.0.2"
    source_git_ref = "cassandra-5.0.2"
    ring_namespace = "cassraw-19537"
    startup_prelude = "sed -ri 's/^(tombstone_warn_threshold:).*/\\1 1/' /etc/cassandra/cassandra.yaml || true"

    root_cause_file = "src/java/org/apache/cassandra/transport/CBUtil.java"
    root_cause_description = (
        "Protocol warning strings containing the non-BMP character U+10FFFF are length-prefixed with a UTF-8 "
        "size computed from UTF-16 code units. A tombstone warning that echoes such a query misaligns the "
        "response frame, and cqlsh's bundled driver raises 'Unknown RESULT kind: 131072'."
    )
    bug_pattern = r"Unknown RESULT kind: 131072"

    def observe_bug(self) -> str:
        high = chr(0x10FFFF)
        cql = f"""
        DROP KEYSPACE IF EXISTS ks19537;
        CREATE KEYSPACE ks19537 WITH replication = {{'class':'SimpleStrategy','replication_factor':1}};
        CREATE TABLE ks19537.t (pk int, ck text, v int, PRIMARY KEY (pk, ck));
        INSERT INTO ks19537.t (pk, ck, v) VALUES (0, '{high}a', 1);
        INSERT INTO ks19537.t (pk, ck, v) VALUES (0, '{high}b', 2);
        INSERT INTO ks19537.t (pk, ck, v) VALUES (0, '{high}c', 3);
        INSERT INTO ks19537.t (pk, ck, v) VALUES (0, '{high}d', 4);
        DELETE FROM ks19537.t WHERE pk = 0 AND ck = '{high}a';
        DELETE FROM ks19537.t WHERE pk = 0 AND ck = '{high}b';
        DELETE FROM ks19537.t WHERE pk = 0 AND ck = '{high}c';
        DELETE FROM ks19537.t WHERE pk = 0 AND ck = '{high}d';
        SELECT * FROM ks19537.t WHERE pk = 0 AND ck >= '{high}' LIMIT 100 ALLOW FILTERING;
        """
        return self.cql_file(cql, timeout=180)
