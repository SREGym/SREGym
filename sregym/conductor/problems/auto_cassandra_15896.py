"""CASSANDRA-15896: NullPointerException in SELECT JSON when a UUID column holds an empty string.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-15896

Buggy: 3.11.7  ->  Fixed: 3.11.8 (also 3.0.22, 4.0-beta2, 4.0).

Reproduction (single node, pure CQL via cqlsh):
  1. Create a table with a uuid column.
  2. INSERT ... JSON with an empty string "" (NOT null) for that uuid field — Cassandra
     accepts it and stores a zero-length value.
  3. SELECT JSON of that row (selecting the empty-UUID column) — the server throws a
     NullPointerException while serializing the empty UUID to JSON.

Verbatim buggy signature (from the reproduction evidence log):

  Client (cqlsh):
    <stdin>:5:ServerError: java.lang.NullPointerException

  Server log:
    java.lang.NullPointerException: null
        at org.apache.cassandra.db.marshal.AbstractType.toJSONString(AbstractType.java:156)
        at org.apache.cassandra.cql3.selection.Selection.rowToJson(Selection.java:343)
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra15896(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    root_cause_file = "src/java/org/apache/cassandra/db/marshal/AbstractType.java"
    root_cause_description = (
        "SELECT JSON over a UUID column that holds an empty string (zero-length value, not null) "
        "throws java.lang.NullPointerException server-side at "
        "AbstractType.toJSONString(AbstractType.java:156), reached via "
        "Selection.rowToJson(Selection.java:343) while building the JSON result. The empty/zero-length "
        "buffer deserializes to a null UUID, which is then dereferenced during JSON serialization. The "
        "client (cqlsh) sees 'ServerError: java.lang.NullPointerException'."
    )
    reproducer = """
CREATE KEYSPACE IF NOT EXISTS repro15896 WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro15896.t (id uuid PRIMARY KEY, another_id uuid, subject text);
INSERT INTO repro15896.t JSON '{"id":"11111111-1111-1111-1111-111111111111","another_id":"","subject":"dante"}';
SELECT JSON id, another_id FROM repro15896.t;
"""
    continuous_reproducer = True
    # 3.11.7 already ships the bug (fix landed in 3.11.8), so deploy the stock image
    # instead of an ant-jar source build.
    prebuilt_from_stock = True
