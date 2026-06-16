"""CASSANDRA-19475: system_views.settings incorrectly handles array types.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-19475

Title: system_views.settings incorrectly handle array types.

Buggy: 4.1.4. Fixed: 4.1.5 (also 5.0-rc1, 5.0, 6.0-alpha1, 6.0).

Reproduction (single node, pure CQL — virtual tables are on by default in 4.1,
NOT config-gated):
  Query the system_views.settings virtual table for an array-typed config setting,
  e.g. data_file_directories (a String[]). `name` is the partition key, so no
  ALLOW FILTERING is needed. On 4.1.4 the value column renders the Java array's
  default Object.toString() (a JVM identity hash) instead of the directory list.
  The scalar setting commitlog_directory renders correctly on both builds,
  confirming the defect is specific to array-typed settings.

Wrong-result bug (no exception): the buggy build returns an incorrect value rather
than erroring. `expected_output` is set to the BUGGY value's stable prefix so the
mitigation probe greps for it (Ready = bug present, NotReady = fixed). The trailing
hex (`@4cb1c088`) is a per-run JVM identity hashcode and is intentionally excluded
from `expected_output`; the stable, telling token is the `[Ljava.lang.String;@`
prefix.

Verbatim buggy signature (from the reproduction evidence log):
  data_file_directories | [Ljava.lang.String;@4cb1c088
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra19475(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.4"
    source_git_ref = "cassandra-4.1.4"
    # 4.1.4 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/virtual/SettingsTable.java"
    root_cause_description = (
        "Querying the system_views.settings virtual table for an array-typed config "
        "setting (e.g. data_file_directories, a String[]) renders the value as the "
        "Java array's default Object.toString() — '[Ljava.lang.String;@<hash>' — "
        "instead of the directory list. The root cause is in SettingsTable.java: "
        "getValue(Property) calls value.toString() directly on the setting value "
        "without checking value.getClass().isArray(), so arrays fall through to the "
        "JVM identity-hash toString(). The fix special-cases array types via "
        "Arrays.asList((Object[]) value).toString(). Scalar settings such as "
        "commitlog_directory are unaffected."
    )

    reproducer = """
SELECT name, value FROM system_views.settings WHERE name = 'data_file_directories';
"""
    continuous_reproducer = True

    # Wrong-result bug: the mitigation probe greps (grep -qF) the reproducer output
    # for this BUGGY token. It is the stable prefix of the Java array toString();
    # the per-run identity-hash suffix (e.g. @4cb1c088) is deliberately omitted so
    # the fixed build's correct value ('[/.../data/data]') never matches.
    expected_output = "[Ljava.lang.String;@"
