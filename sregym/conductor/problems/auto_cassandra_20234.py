"""CASSANDRA-20234: protocol-v4 warning serialization has an off-by-one size bug."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra20234(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "5.0.2"
    cassandra_version = "5.0.2"
    source_git_ref = "cassandra-5.0.2"
    ring_namespace = "cassraw-20234"
    startup_prelude = "sed -ri 's/^(tombstone_warn_threshold:).*/\\1 1/' /etc/cassandra/cassandra.yaml || true"

    root_cause_file = "src/java/org/apache/cassandra/transport/messages/ResultMessage.java"
    root_cause_description = (
        "The track-warnings serialization path underestimates or overestimates warning string length for "
        "non-BMP query text. With protocol v4 and a tombstone warning containing U+10FFFF, 5.0.2 misaligns "
        "the result frame and cqlsh raises 'Unknown RESULT kind: 131072'."
    )
    bug_pattern = r"Unknown RESULT kind: 131072"

    def observe_bug(self) -> str:
        high = chr(0x10FFFF)
        cql = f"""
        DROP KEYSPACE IF EXISTS ks20234;
        CREATE KEYSPACE ks20234 WITH replication = {{'class':'SimpleStrategy','replication_factor':1}};
        CREATE TABLE ks20234.t (pk int, ck text, v int, PRIMARY KEY (pk, ck));
        INSERT INTO ks20234.t (pk, ck, v) VALUES (0, '{high}a', 1);
        INSERT INTO ks20234.t (pk, ck, v) VALUES (0, '{high}b', 2);
        INSERT INTO ks20234.t (pk, ck, v) VALUES (0, '{high}c', 3);
        INSERT INTO ks20234.t (pk, ck, v) VALUES (0, '{high}d', 4);
        DELETE FROM ks20234.t WHERE pk = 0 AND ck = '{high}a';
        DELETE FROM ks20234.t WHERE pk = 0 AND ck = '{high}b';
        DELETE FROM ks20234.t WHERE pk = 0 AND ck = '{high}c';
        DELETE FROM ks20234.t WHERE pk = 0 AND ck = '{high}d';
        SELECT * FROM ks20234.t WHERE pk = 0 AND ck >= '{high}' LIMIT 100 ALLOW FILTERING;
        """
        return self.cql_file(cql, timeout=180, protocol_args=" --protocol-version=4")
