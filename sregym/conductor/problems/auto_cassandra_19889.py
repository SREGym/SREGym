"""CASSANDRA-19889: Indexing a frozen collection that is the clustering key and reversed is rejected.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-19889

Buggy: 5.0.1. Fixed: 5.0.2 (also 4.0.15, 4.1.8, 6.0).

Reproduction (single-node, pure CQL):
  1. CREATE TABLE with a frozen<list<int>> column `ck` as the clustering key,
     WITH CLUSTERING ORDER BY (ck DESC).
  2. CREATE INDEX ON tbl(FULL(ck)) — the DESC ordering wraps `ck` in ReverseType,
     which the FULL()-index validation fails to unwrap, so a valid index on a
     frozen collection is wrongly rejected.

The buggy 5.0.1 build rejects the CREATE INDEX with (verbatim signature from the log):
  <stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="full() indexes can only be created on frozen collections"

Removing the DESC clustering order makes the identical FULL(ck) index succeed on the
same buggy node, isolating the trigger to ReverseType. Fixed 5.0.2 accepts the DDL as-is.

Root cause: CreateIndexStatement.validateIndexTarget() checks isFrozenCollection() on the
clustering column's type without unwrapping the ReverseType wrapper introduced by
CLUSTERING ORDER BY (ck DESC), so the index target appears non-frozen and is rejected via
AlterSchemaStatement.ire("full() indexes can only be created on frozen collections").
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra19889(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.1"
    source_git_ref = "cassandra-5.0.1"
    # 5.0.1 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True
    root_cause_file = "src/java/org/apache/cassandra/cql3/statements/schema/CreateIndexStatement.java"
    root_cause_description = (
        "CREATE INDEX ON tbl(FULL(ck)) on a frozen<list<int>> clustering column that uses DESC "
        "ordering (CLUSTERING ORDER BY (ck DESC)) is wrongly rejected with InvalidRequest: "
        "'full() indexes can only be created on frozen collections'. The root cause is in "
        "CreateIndexStatement.validateIndexTarget(): the DESC clustering order wraps the column "
        "type in ReverseType, but the FULL()-index validation checks isFrozenCollection() without "
        "unwrapping the ReverseType, so the (valid) frozen-collection index target appears "
        "non-frozen and is rejected."
    )
    reproducer = """
DROP KEYSPACE IF EXISTS repro19889_ks;
CREATE KEYSPACE repro19889_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
USE repro19889_ks;
CREATE TABLE tbl (
  pk int,
  ck frozen<list<int>>,
  value int,
  PRIMARY KEY (pk, ck)
) WITH CLUSTERING ORDER BY (ck DESC);
CREATE INDEX ON tbl(FULL(ck));
"""
    continuous_reproducer = True
