"""Confirmed deterministic: https://issues.apache.org/jira/browse/CASSANDRA-20050

Title: INSERT fails with InvalidRequest for frozen UDT clustering keys in DESC order.

Buggy: 4.0.14 and 4.1.7. Fixed: 4.0.15 and 4.1.8.

Reproduction:
  1. Create a frozen UDT.
  2. Use it as a clustering column with CLUSTERING ORDER BY (... DESC).
  3. Insert a UDT literal.

Buggy versions reject the INSERT with:
  Invalid user type literal for loc of type frozen<point>

Root cause: UserTypes.java treats a DESC clustering column's ReversedType wrapper
as the user type instead of unwrapping it before validating the UDT literal.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra20050(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.14"
    source_git_ref = "cassandra-4.0.14"
    root_cause_file = "src/java/org/apache/cassandra/cql3/UserTypes.java"
    root_cause_description = (
        "INSERT with a UDT literal into a table whose frozen<UDT> clustering column uses DESC ordering "
        "(CLUSTERING ORDER BY (loc DESC)) fails with InvalidRequest: 'Invalid user type literal for loc "
        "of type frozen<point>'. The root cause is in UserTypes.java: DESC clustering order wraps the "
        "column type in ReversedType, but the UDT literal validation path casts the wrapped type directly "
        "instead of calling unwrap() before treating it as a UserType."
    )
    reproducer = """
DROP KEYSPACE IF EXISTS udt_ks;
CREATE KEYSPACE udt_ks WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};
USE udt_ks;
CREATE TYPE point (x int, y int);
CREATE TABLE events (
  id int,
  loc frozen<point>,
  val text,
  PRIMARY KEY (id, loc)
) WITH CLUSTERING ORDER BY (loc DESC);
INSERT INTO events (id, loc, val) VALUES (1, {x: 10, y: 20}, 'data');
"""
    continuous_reproducer = True
    # 4.0.14 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True
