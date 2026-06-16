"""CASTing a float to decimal adds spurious extra digits.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-18647
Buggy: 4.1.2  ->  Fixed: 4.1.3  (also 3.11.16, 4.0.11, 5.0-alpha1, 5.0)

Reproduction (single node, pure CQL/semantics):
  Create a table with a 32-bit ``float`` column ``e``, insert ``5.2``, then run
  ``SELECT CAST(e AS decimal)``. The buggy build routes float -> double -> decimal,
  widening the 32-bit float to a 64-bit double first, so the decimal cast carries the
  double-widening artifacts. ``CAST(e AS text)`` is unaffected and correctly returns ``5.2``.

Verbatim buggy signature (CAST(e AS decimal) on float 5.2):
  5.199999809265137
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra18647(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.2"
    source_git_ref = "cassandra-4.1.2"
    # 4.1.2 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/cql3/functions/CastFcts.java"
    root_cause_description = (
        "CAST(<float> AS decimal) returns spurious extra digits (e.g. 5.199999809265137 for a "
        "float holding 5.2). In CastFcts.java, getDecimalConversionFunction() treats FloatType "
        "and DoubleType identically with 'p -> BigDecimal.valueOf(p.doubleValue())'. For a float "
        "this widens the 32-bit value to a 64-bit double first, so the decimal cast faithfully "
        "captures the double-widening binary artifacts. The fix handles FloatType separately via "
        "'new BigDecimal(Float.toString(p.floatValue()))' so the decimal reflects the float's own "
        "string form (5.2). CAST(<float> AS text) was already correct."
    )

    # Single-node wrong-result bug: the final SELECT returns an incorrect value (no exception).
    # The continuous reproducer loops this CQL; the readiness probe greps the cqlsh output for
    # expected_output, so Ready = buggy value still present, NotReady = fixed.
    reproducer = """
CREATE KEYSPACE IF NOT EXISTS repro18647 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro18647.tbl (p int PRIMARY KEY, e float);
INSERT INTO repro18647.tbl (p, e) VALUES (1, 5.2);
SELECT CAST(e AS decimal) FROM repro18647.tbl WHERE p=1;
"""
    continuous_reproducer = True

    # Wrong-result bug: the BUGGY value the 4.1.2 build returns for CAST(e AS decimal) on float 5.2.
    # The mitigation probe greps for this string (grep -qF), so a fixed build (returning 5.2) makes
    # the reproducer pod go NotReady.
    expected_output = "5.199999809265137"
