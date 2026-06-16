"""CASSANDRA-17918: DESCRIBE output does not quote column names using reserved keywords.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17918
Buggy: 4.1.1   Fixed: 4.1.2 (also 4.0.10, 5.0-alpha1, 5.0)
Fix commit: 75194201f1f06d120f246f6fad025ca5f672943d

Reproduction (single node, pure CQL):
  Create a UDT whose field names are reserved keywords ("token", "desc"), then run
  DESCRIBE TYPE. On 4.1.1 the field names are emitted UNQUOTED, so feeding the DESCRIBE
  output back as a CREATE TYPE fails to re-import (SyntaxException). On 4.1.2 the same
  field names are correctly emitted quoted ("token" / "desc") and round-trip cleanly.

This is a WRONG-RESULT bug: DESCRIBE TYPE returns an incorrect value rather than raising.
The buggy DESCRIBE output emits the reserved keyword field name without quotes, e.g.
(verbatim buggy signature; control 4.1.2 emits `    "token" text,`):

    token text,

Root cause: src/java/org/apache/cassandra/db/marshal/UserType.java — UserType.toCqlString()
(reached via SchemaElement.toCqlString for DESCRIBE TYPE) appended UDT field names with
plain `builder.append(fieldNameAsString(i))` instead of `builder.appendQuotingIfNeeded(...)`,
so field names that are reserved keywords are not quoted in the emitted CREATE TYPE.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra17918(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.1"
    source_git_ref = "cassandra-4.1.1"
    # 4.1.1 already ships the bug (fix landed in 4.1.2), so deploy the stock image
    # instead of running a ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/marshal/UserType.java"
    root_cause_description = (
        "DESCRIBE TYPE emits UDT field names that are reserved keywords WITHOUT quoting, so the "
        "described schema cannot be re-imported (CREATE TYPE on the output fails with a "
        "SyntaxException). The root cause is in UserType.toCqlString() (reached via "
        "SchemaElement.toCqlString on the DESCRIBE-output path): it appended each field name with "
        "plain `builder.append(fieldNameAsString(i))` instead of `builder.appendQuotingIfNeeded(...)`, "
        'so reserved-keyword field names like "token" and "desc" are emitted unquoted. The fix '
        "routes the field name through appendQuotingIfNeeded so reserved keywords are quoted."
    )

    # Wrong-result reproducer. The continuous reproducer pod re-runs this block in a loop, so it
    # must be loop-idempotent: DROP the keyspace first, then recreate the keyspace + UDT and DESCRIBE.
    # The "token"/"desc" quotes in the CREATE TYPE *input* are mandatory (reserved keywords); the bug
    # is only in the DESCRIBE *output*, where 4.1.1 drops those quotes.
    reproducer = """
DROP KEYSPACE IF EXISTS repro17918;
CREATE KEYSPACE repro17918 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TYPE repro17918.t ("token" text, "desc" text);
DESCRIBE TYPE repro17918.t;
"""
    continuous_reproducer = True

    # Wrong-result bug: this is the BUGGY value the buggy DESCRIBE output contains (the reserved
    # keyword `token` emitted unquoted). The fixed 4.1.2 output is `    "token" text,`, which does
    # NOT contain the substring "token text," — so the readiness probe greps for this string:
    # Ready = bug still present (unquoted), Not Ready = fixed (quoted).
    expected_output = "token text,"
