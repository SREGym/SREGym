"""CASSANDRA-16868: Secondary indexes on primary-key columns can miss some writes.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16868
Buggy: 4.0.0  ->  Fixed: 3.0.26 / 3.11.12 / 4.0.1 / 4.1
Component: Feature/2i Index

Reproduction (single node, pure CQL, in-memtable — no flush):
  Create a table with a clustering key, add a secondary index on that clustering
  column, INSERT a row (creates the index entry), DELETE the row, then UPDATE the
  same primary-key row back to life. On the buggy build the UPDATE reuses the
  *deleted* row's LivenessInfo in CassandraIndex.updateRow (instead of
  getPrimaryKeyIndexLiveness, as insertRow does), so the row becomes LIVE again but
  NO index entry is ever created. A SELECT filtering on the indexed column then
  returns nothing even though the row exists (a PK lookup still returns it).

Verbatim buggy signature (the indexed lookup returns 0 rows on 4.0.0, while the
PK lookup and the 4.0.1 control both return the live row (1, 2, 3)):

    SELECT * FROM repro16868.t WHERE ck=2;
     pk | ck | v
    ----+----+---

    (0 rows)

This is a WRONG-RESULT / silent data-correctness bug — there is no stack trace.
The mitigation probe greps the reproducer output for "(0 rows)": present on the
buggy build (Ready = bug still present), absent on the fixed build where the
indexed SELECT returns "(1 rows)" (NotReady = bug fixed).
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16868(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.0"
    source_git_ref = "cassandra-4.0.0"
    # 4.0.0 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/index/internal/CassandraIndex.java"
    root_cause_description = (
        "Secondary indexes on primary-key (clustering) columns can miss writes. In "
        "CassandraIndex.java, updateRow() reuses the LivenessInfo of the previously "
        "deleted row instead of calling getPrimaryKeyIndexLiveness(newRow) the way "
        "insertRow() does. When an UPDATE lands on a primary-key row that was just "
        "DELETEd, the row is brought back to life but no index entry is created, so a "
        "SELECT filtering on the indexed PK-component column returns no rows even though "
        "the row exists. The fix is to use getPrimaryKeyIndexLiveness in updateRow."
    )

    # Single-node, pure CQL, in-memtable (no flush — exactly as the Jira body's first
    # example). This block is run both as the fault trigger and, in a loop, as the
    # mitigation readiness probe, so it is wrapped in DROP/CREATE KEYSPACE + USE to be
    # self-contained and idempotent on every iteration. The INSERT -> DELETE -> UPDATE
    # sequence re-establishes the exact buggy state each run regardless of prior state.
    # The final SELECT (the discriminator the probe greps for "(0 rows)") is appended
    # here; the evidence log ran it separately from the setup workload.
    reproducer = """
DROP KEYSPACE IF EXISTS repro16868;
CREATE KEYSPACE repro16868 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
USE repro16868;
CREATE TABLE t (pk int, ck int, v int, PRIMARY KEY (pk, ck));
CREATE INDEX ON t(ck);
INSERT INTO t(pk, ck, v) VALUES (1, 2, 3);
DELETE FROM t WHERE pk = 1 AND ck = 2;
UPDATE t SET v = 3 WHERE pk = 1 AND ck = 2;
SELECT * FROM t WHERE ck = 2;
"""
    continuous_reproducer = True
    # Wrong-result bug: the BUGGY indexed SELECT returns "(0 rows)" (no index entry),
    # whereas the fixed build returns "(1 rows)". The probe greps for this buggy value,
    # so Ready = bug still present, NotReady = fixed.
    expected_output = "(0 rows)"
