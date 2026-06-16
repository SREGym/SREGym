"""Order by descending on frozen list not working: https://issues.apache.org/jira/browse/CASSANDRA-15814

Title: ORDER BY descending on a frozen<list<...>> clustering column rejects list-literal inserts.

Buggy: 3.11.7. Fixed: 3.11.8 (also 2.2.18, 3.0.22, 4.0-beta2, 4.0).

Reproduction (single node, pure CQL):
  1. Create a table with a frozen<list<int>> clustering column and WITH CLUSTERING ORDER BY (version DESC).
     The table is created successfully.
  2. INSERT a list literal into that clustering column -> InvalidRequest.
The identical INSERT succeeds against a table with no clustering order (default ASC), so the trigger is
specifically the DESC clustering-order clause on the frozen-list clustering column.

Verbatim buggy signature (3.11.7):
  InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid list literal for version of type frozen<list<int>>"

Root cause: with DESC clustering order the column type is wrapped in ReversedType, but
Lists.Literal.validateAssignableTo (src/java/org/apache/cassandra/cql3/Lists.java) checks
`receiver.type instanceof ListType` against the wrapped ReversedType instead of unwrapping it first,
so a valid frozen<list<int>> literal is rejected. The fix adds an unwrap() of ReversedType before the
ListType check (present in 3.11.8+).
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra15814(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    root_cause_file = "src/java/org/apache/cassandra/cql3/Lists.java"
    root_cause_description = (
        "INSERT of a list literal into a table whose frozen<list<int>> clustering column uses DESC "
        "ordering (WITH CLUSTERING ORDER BY (version DESC)) fails with InvalidRequest: 'Invalid list "
        "literal for version of type frozen<list<int>>'. The root cause is in Lists.java: DESC clustering "
        "order wraps the column type in ReversedType, but Lists.Literal.validateAssignableTo checks "
        "`receiver.type instanceof ListType` against the wrapped ReversedType instead of unwrapping it "
        "first, so the list literal is rejected. The same INSERT succeeds against an ASC (default-order) "
        "table. The fix unwraps ReversedType before the ListType check (3.11.8+)."
    )
    reproducer = """
DROP KEYSPACE IF EXISTS repro_15814;
CREATE KEYSPACE repro_15814 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
USE repro_15814;
CREATE TABLE software_desc (
  name ascii,
  version frozen<list<int>>,
  data ascii,
  PRIMARY KEY (name, version)
) WITH CLUSTERING ORDER BY (version DESC);
INSERT INTO software_desc (name, version) VALUES ('t1', [2,10,30,40,50]);
"""
    continuous_reproducer = True
    # 3.11.7 already ships the bug (fixed in 3.11.8), so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True
