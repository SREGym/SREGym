"""CASSANDRA-19891: SAI query fails when a non-indexed CompositeType column embeds a MapType.

Title: SAI fails queries when multiple columns exist and a non-indexed column is a
CompositeType with a MapType inside.
JIRA: https://issues.apache.org/jira/browse/CASSANDRA-19891
Component: Feature/2i Index (Storage-Attached Indexing).

Buggy: 5.0.4   Fixed: 5.0.5 (also 6.0-alpha1, 6.0).

Reproduction (single node, RF=1, pure CQL — table stays empty; the bug fires at
query-plan time before any data is read):
  1. CREATE TABLE with 11 columns; the load-bearing one is the *non-indexed* column
     "r5" 'CompositeType(CompositeType(ShortType,SimpleDateType,BooleanType),CompositeType(FloatType),MapType(ByteType,TimeType))'.
  2. CREATE 5 SAI indexes: FULL("ck1"), FULL("pk1"), FULL("r4"), "r2", "r3".
  3. A single-column SAI query (WHERE "r3" = 0x... ALLOW FILTERING) SUCCEEDS.
  4. A multi-column query that also restricts the non-indexed "r5"
     (WHERE "r5" = 0x... AND "r3" = 0x... AND "r2" = 0x... AND "pk2" = ((-1.2651989E-23)) ALLOW FILTERING)
     FAILS: SAI builds an *unindexed* expression for "r5", whose CompositeType embeds a
     map, and IndexTermType cannot handle the embedded map.

The client sees a generic ReadFailure (code=1300, "1 failures: UNKNOWN"); the
discriminating signature is server-side. Verbatim buggy signature:

  Caused by: java.lang.IllegalArgumentException: Unsupported collection type: map
        at org.apache.cassandra.index.sai.utils.IndexTermType.collectionCellValueType(IndexTermType.java:789)
        at org.apache.cassandra.index.sai.utils.IndexTermType.calculateIndexType(IndexTermType.java:726)
        ...
        at org.apache.cassandra.index.sai.plan.Operation.buildUnindexedExpression(Operation.java:163)
        at org.apache.cassandra.index.sai.plan.Operation.buildIndexExpressions(Operation.java:139)
        ...
        at org.apache.cassandra.index.sai.plan.StorageAttachedIndexSearcher.search(StorageAttachedIndexSearcher.java:116)

A/B control: the identical schema + identical multi-column query on 5.0.5 succeeds
(0 rows) with zero occurrences of the exception in the server log.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra19891(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.4"
    source_git_ref = "cassandra-5.0.4"
    # 5.0.4 already ships the bug (fix landed in 5.0.5), so deploy the stock image
    # instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/index/sai/utils/IndexTermType.java"
    root_cause_description = (
        "A multi-column SAI query (ALLOW FILTERING) that restricts a non-indexed column whose type is a "
        "CompositeType embedding a MapType fails with 'IllegalArgumentException: Unsupported collection "
        "type: map'. During SAI query-plan construction, Operation.buildUnindexedExpression creates an "
        "IndexTermType for the non-indexed column 'r5' "
        "('CompositeType(CompositeType(ShortType,SimpleDateType,BooleanType),CompositeType(FloatType),"
        "MapType(ByteType,TimeType))'); IndexTermType.collectionCellValueType (IndexTermType.java:789) "
        "does not handle the embedded map and throws, surfacing to the client as a ReadFailure "
        "(code=1300, '1 failures: UNKNOWN'). A single-column query on an indexed column on the same table "
        "succeeds — only the multi-column case that pulls in the non-indexed CompositeType-with-MapType "
        "column trips the bug."
    )

    # Schema is created exactly once, before the reproducer loop, by
    # setup_preconditions() (inject_fault runs it after swapping in the buggy image
    # and before deploy_continuous_reproducer). Keeping the table + SAI index DDL
    # here — rather than inside `reproducer` — means the continuously-looping
    # reproducer re-runs ONLY the failing SELECT, so the readiness probe's exit code
    # reflects solely the bug (buggy: SELECT fails -> NotReady; fixed: SELECT
    # succeeds -> Ready) instead of being polluted by "table already exists" errors.
    # Verbatim from the reproduction evidence log (/tmp/repro_schema.cql).
    _setup_preconditions_sql = """
CREATE KEYSPACE IF NOT EXISTS keyspace_test_19891 WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};

CREATE TABLE keyspace_test_19891."tbl" (
   "pk1" frozen<map<'CompositeType(IntegerType,SimpleDateType)', 'DynamicCompositeType(Q=>LongType,I=>ByteType,6=>LexicalUUIDType)'>>,
   "pk2" frozen<tuple<frozen<tuple<float>>>>,
   "ck1" frozen<list<frozen<map<'LexicalUUIDType', ascii>>>>,
   "ck2" tinyint,
   "r1" frozen<list<'DynamicCompositeType(X=>DecimalType,y=>TimestampType,f=>BooleanType)'>> static,
   "r2" 'DynamicCompositeType(P=>ShortType)',
   "r3" 'CompositeType(FrozenType(ListType(DoubleType)),FrozenType(MapType(LongType,DurationType)),DoubleType)',
   "r4" frozen<list<frozen<list<time>>>>,
   "r5" 'CompositeType(CompositeType(ShortType,SimpleDateType,BooleanType),CompositeType(FloatType),MapType(ByteType,TimeType))',
   "r6" set<smallint>,
   PRIMARY KEY (("pk1", "pk2"), "ck1", "ck2")
) WITH CLUSTERING ORDER BY ("ck1" ASC, "ck2" DESC);

CREATE INDEX ON keyspace_test_19891."tbl"(FULL("ck1")) USING 'SAI';
CREATE INDEX ON keyspace_test_19891."tbl"(FULL("pk1")) USING 'SAI';
CREATE INDEX ON keyspace_test_19891."tbl"(FULL("r4")) USING 'SAI';
CREATE INDEX ON keyspace_test_19891."tbl"("r2") USING 'SAI';
CREATE INDEX ON keyspace_test_19891."tbl"("r3") USING 'SAI';
"""

    # The multi-column query that triggers the bug, verbatim from the evidence log
    # (/tmp/repro_multi.cql). Fully qualified (keyspace_test_19891."tbl"), so the
    # looping reproducer needs no USE. Restricting the non-indexed "r5"
    # (CompositeType-with-MapType) alongside the SAI-indexed "r3"/"r2" makes SAI build
    # an unindexed expression for "r5" and throw. The within-version single-column
    # control query (WHERE "r3" = 0x... ALLOW FILTERING, which SUCCEEDS) is
    # intentionally excluded.
    reproducer = """
SELECT *
FROM keyspace_test_19891."tbl"
WHERE "r5" = 0x0010000230bd00000457f0bd31000001000000000700049f647252000000260000000200000001f300000008000001c4e14bba4b00000001260000000800003f2b300d385d00
    AND "r3" = 0x001c00000002000000083380d171eace676900000008e153bb97fdd5c22e00006d000000030000000897c5493857999fc000000013f08cc4fad0f04d0de51cff28d4ae743d2da1c40000000857108e8c372c868400000013f0cc6bca55f0ee240b27ff12c77a7b7dc3c665000000086c07d25fcdd3403500000013f0745922bdf0ac44c9b5ffd80f025ded9a211d000008200547f5da7a43aa00
    AND "r2" = 0x8050000255e200
    AND "pk2" = ((-1.2651989E-23))
ALLOW FILTERING;
"""
    continuous_reproducer = True
