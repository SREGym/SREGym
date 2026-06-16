"""Nested selection of reversed collections fails: https://issues.apache.org/jira/browse/CASSANDRA-17913

Title: Nested selection of reversed collections fails.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17913

Buggy: 4.1.1. Fixed: 4.0.10 / 4.1.2 / 5.0. (Introduced by CASSANDRA-7396 in 4.0.)

Reproduction (single node, pure CQL — fires at statement preparation, no data / no WHERE needed):
  1. Create a table whose clustering column is a frozen<map<text,int>> declared
     WITH CLUSTERING ORDER BY (c DESC) — DESC wraps the type in ReversedType.
  2. Run element selection `SELECT c['testing'] FROM t` (or slice selection
     `SELECT c['a'..'z'] FROM t`).

The buggy build rejects the SELECT at prepare time with:
  InvalidRequest: Error from server: code=2200 [Invalid query]
  message="Invalid element selection: c is of type frozen<map<text, int>> is not a collection"

The fixed build (4.1.2) returns 0 rows on the identical query.

Root cause: Selectable.WithElementSelection / Selectable.WithSliceSelection check whether the
column is a collection without unwrapping the ReversedType wrapper applied by DESC clustering
order, so a frozen-collection clustering column declared DESC (real type ReversedType(MapType(...)))
is wrongly treated as a non-collection.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra17913(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.1"
    source_git_ref = "cassandra-4.1.1"
    root_cause_file = "src/java/org/apache/cassandra/cql3/selection/Selectable.java"
    root_cause_description = (
        "Element selection (c['key']) or slice selection (c['a'..'z']) on a frozen collection "
        "clustering column declared WITH CLUSTERING ORDER BY (c DESC) fails at statement preparation "
        "with InvalidRequest: 'Invalid element selection: c is of type frozen<map<text, int>> is not a "
        "collection'. DESC clustering order wraps the column type in ReversedType, but the collection-type "
        "checks in Selectable.WithElementSelection / Selectable.WithSliceSelection do not unwrap the "
        "ReversedType before deciding whether the column is a collection, so a reversed frozen "
        "collection (real type ReversedType(MapType(...))) is wrongly rejected as not a collection."
    )
    # continuous_reproducer loops this whole block, so DROP KEYSPACE IF EXISTS first to keep
    # CREATE KEYSPACE idempotent — otherwise iteration 2+ fails with "keyspace already exists"
    # and the mitigation oracle could never detect a fix.
    reproducer = """
DROP KEYSPACE IF EXISTS repro17913_ks;
CREATE KEYSPACE repro17913_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro17913_ks.t (k int, c frozen<map<text,int>>, v int, PRIMARY KEY(k,c))
  WITH CLUSTERING ORDER BY (c DESC);
SELECT c['testing'] FROM repro17913_ks.t;
"""
    continuous_reproducer = True
    # 4.1.1 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True
