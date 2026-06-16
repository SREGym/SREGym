"""CASSANDRA-19637: LWT conditions behavior on collections is inconsistent.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-19637
Buggy: 4.1.5  ->  Fixed: 4.1.6 (also 4.0.14, 5.0-rc1).

Reproduction summary (wrong-result / case (a) headline, non-frozen list):
  On a row whose non-frozen `list<int>` column `l` is NULL, the LWT conditional
  `UPDATE ... IF l < [1,2]` is mis-evaluated: the buggy 4.1.5 build APPLIES the
  write and returns `[applied]=True`, whereas a frozen<list> with the same NULL
  value (and the fixed 4.1.6 build) correctly returns `[applied]=False`. The
  reproducer self-resets the row to `l = NULL` (DELETE + INSERT k) on every run
  so each iteration deterministically yields the buggy `True` (buggy) vs `False`
  (fixed).

Verbatim buggy signature (cassandra:4.1.5), NON-FROZEN table tn, k=0, l NULL:
    $ cqlsh -e "UPDATE repro19637.tn SET l=[9] WHERE k=0 IF l < [1,2];"

     [applied]
    -----------
          True
  (FROZEN table, same NULL value & same condition, returns False/null; on the
  fixed 4.1.6 build the NON-FROZEN case also returns False/null — the
  inconsistency is resolved.)

Verbatim signature, case (b) (the second documented inconsistency, NON-FROZEN
list, same column, NULL value):
    `UPDATE ... IF l >= null` -> InvalidRequest code=2200 message="Invalid
    comparison with null for operator ">="" ; `UPDATE ... IF l >= []` ->
    [applied]=True. (On 4.1.6 the `>= []` case throws InvalidRequest "Invalid
    comparison with an empty list for operator >=".)
  The literal string "Invalid comparison with null for operator" lives in the
  root-cause file ColumnCondition.java (pinned via fix commit
  90208c0a29157fdc4ac88d7e24708535650b5d55), tying this signature to that file.

Root cause (src/java/org/apache/cassandra/cql3/conditions/ColumnCondition.java):
  The condition-evaluation path for multi-cell (non-frozen) collections does not
  treat a NULL / empty multi-cell collection input the same way it treats a
  single-cell (frozen) collection, so `IF l < [1,2]` against a NULL non-frozen
  column is evaluated inconsistently with the frozen case. The fix treats an
  empty multi-cell collection input as null and rejects null input for operators
  other than `=` / `!=`.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra19637(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.5"
    source_git_ref = "cassandra-4.1.5"
    # 4.1.5 already ships the bug (fix landed in 4.1.6), so deploy the stock
    # image instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/cql3/conditions/ColumnCondition.java"
    root_cause_description = (
        "LWT conditions on collections are evaluated inconsistently between frozen (single-cell) "
        "and non-frozen (multi-cell) list columns when the column value is NULL. In "
        "ColumnCondition.java, an LWT condition such as IF l < [1,2] against a NULL non-frozen "
        "list<int> column is mis-evaluated so the conditional UPDATE is APPLIED (returns "
        "[applied]=True), whereas a frozen<list> column with the same NULL value correctly "
        "returns [applied]=False. The fix treats an empty multi-cell collection input as null and "
        "rejects null input for operators other than = / !=, making the non-frozen path consistent "
        "with the frozen path."
    )

    # Wrong-result reproducer (case (a), non-frozen). Self-resetting: DELETE +
    # INSERT(k) restores l = NULL before every conditional UPDATE so each loop
    # iteration is deterministic — buggy 4.1.5 returns [applied]=True, fixed
    # 4.1.6 returns [applied]=False. (The bare UPDATE alone would mutate l to [9]
    # on its first apply and then return False forever, silently masking the bug.)
    reproducer = """
CREATE KEYSPACE IF NOT EXISTS repro19637 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro19637.tn (k int PRIMARY KEY, l list<int>);
DELETE FROM repro19637.tn WHERE k=0;
INSERT INTO repro19637.tn (k) VALUES (0);
UPDATE repro19637.tn SET l=[9] WHERE k=0 IF l < [1,2];
"""
    continuous_reproducer = True

    # Wrong-result bug: the BUGGY [applied] value is True (the fixed build returns
    # False). The reproducer-pod readiness probe greps the cqlsh output for this
    # value with `grep -qF`, so Ready = bug present (True found), NotReady = fixed
    # (only False present). The fixed-build output is `False | null`, which
    # contains no "True", so this discriminator is clean.
    expected_output = "True"
