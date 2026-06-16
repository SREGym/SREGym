"""CASSANDRA-15857: Frozen RawTuple is not annotated with frozen in the toString method.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-15857
Buggy: 3.11.7  ->  Fixed: 3.11.8 (also 4.0-beta2, 4.0)
Component: Legacy/CQL

STUB: offline CQLSSTableWriter Java-API reproduction not encoded as a single-cluster
CQL Problem -- see the full steps in the `reproducer` string below.

Why this is a stub (NOT a flattenable CQL reproducer):
  This bug is ONLY observable through the offline bulk-loader Java API
  (`CQLSSTableWriter`). It is NOT visible from a live server / cqlsh. A running
  Cassandra node prepares UDT field types directly from the AST and never
  serializes-and-re-parses them, so `CREATE TYPE`/`CREATE TABLE` over cqlsh
  SUCCEED SILENTLY. There is no CQL statement you can run against a deployed
  cluster that fires this fault. Reproducing it requires compiling and running a
  small Java program against the buggy 3.11.x JARs (zero running Cassandra nodes
  needed). Therefore this cannot be expressed as a single `reproducer` CQL string,
  and `continuous_reproducer` (which would deploy a CQL-loop pod) does not drive it.

Reproduction summary:
  All raw CQL3 types that support freezing wrap themselves with `frozen<>` in
  `toString()`, EXCEPT `RawTuple`. Since CASSANDRA-15035 a tuple nested inside a
  collection must be explicitly wrapped with `frozen`, else `RawCollection.prepare`
  throws. When `CQLSSTableWriter.Builder.build()` materializes a UDT, it round-trips
  each field type through `CQL3Type.Raw::toString` (in `Types$RawBuilder$RawUDT.prepare`)
  and re-parses it. Because `RawTuple.toString` drops the `frozen<>` wrapper, the
  re-parsed field type for `list<frozen<tuple<text, text>>>` becomes the NON-frozen
  `list<tuple<text, text>>`, and `RawCollection.prepare` throws an
  `InvalidRequestException`. The fix (3.11.8) makes `RawTuple.toString` emit the
  `frozen<>` wrapper so the round-tripped type stays frozen and `build()` succeeds.

Verbatim buggy signature (from the reproduction evidence log):
  org.apache.cassandra.exceptions.InvalidRequestException: Non-frozen tuples are not
  allowed inside collections: list<tuple<text, text>>
    at org.apache.cassandra.cql3.CQL3Type$Raw$RawCollection.throwNestedNonFrozenError(CQL3Type.java:705)
    at org.apache.cassandra.cql3.CQL3Type$Raw$RawCollection.prepare(CQL3Type.java:664)
    at org.apache.cassandra.cql3.CQL3Type$Raw$RawCollection.prepareInternal(CQL3Type.java:656)
    at org.apache.cassandra.schema.Types$RawBuilder$RawUDT.lambda$prepare$2(Types.java:313)
    at org.apache.cassandra.schema.Types$RawBuilder$RawUDT.prepare(Types.java:314)
    at org.apache.cassandra.schema.Types$RawBuilder.build(Types.java:263)
    at org.apache.cassandra.io.sstable.CQLSSTableWriter$Builder.createTypes(CQLSSTableWriter.java:563)
    at org.apache.cassandra.io.sstable.CQLSSTableWriter$Builder.build(CQLSSTableWriter.java:538)
    at Repro.main(Repro.java:22)

Note on the buggy version: the canonical buggy version is 3.11.7 (released fix patch
3.11.8 minus 1). During live reproduction the cassandra:3.11.7 image was unavailable
(Docker Hub rate-limited), so the cached cassandra:3.11.6 JARs were used as a
substitute -- 3.11.6 is on the same pre-fix 3.11 line and exhibits the bug
identically. The Problem itself pins the canonical 3.11.7.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra15857(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    # 3.11.7 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/cql3/CQL3Type.java"
    root_cause_description = (
        "In CQL3Type.java, every raw CQL3 type that supports freezing wraps itself with "
        "frozen<> in toString(), except RawTuple, whose toString() drops the frozen<> "
        "wrapper. When CQLSSTableWriter.Builder.build() materializes a UDT it round-trips "
        "each field type through CQL3Type.Raw::toString (Types$RawBuilder$RawUDT.prepare) "
        "and re-parses it, so a 'list<frozen<tuple<text, text>>>' field is re-parsed as the "
        "non-frozen 'list<tuple<text, text>>' and RawCollection.prepare throws "
        "InvalidRequestException ('Non-frozen tuples are not allowed inside collections'). "
        "Fix: make RawTuple.toString emit the frozen<> wrapper."
    )

    # STUB reproducer: this is an OFFLINE CQLSSTableWriter Java-API bug, NOT a CQL/cqlsh
    # bug, so it cannot be expressed as a single CQL string run against a live cluster.
    # The full, exact steps from the reproduction evidence log are transcribed below.
    reproducer = r"""
STUB: offline CQLSSTableWriter Java-API reproduction -- NOT a single-cluster CQL Problem.
Zero running Cassandra nodes are required; only the buggy 3.11.x JARs + a JDK (8 or 11).
This bug is NOT visible from cqlsh/a live server (CREATE TYPE/CREATE TABLE succeed
silently); the only trigger is the offline bulk-loader Java path below.

--- Repro.java ---
import org.apache.cassandra.config.Config;
import org.apache.cassandra.io.sstable.CQLSSTableWriter;
import java.io.File;

public class Repro {
    public static void main(String[] args) throws Exception {
        Config.setClientMode(true);
        File dir = new File("/tmp/sst");
        dir.mkdirs();
        String createType = "CREATE TYPE ks.footype ( f list<frozen<tuple<text, text>>> )";
        String createTable = "CREATE TABLE ks.t ( id int PRIMARY KEY, v frozen<footype> )";
        String insert = "INSERT INTO ks.t (id, v) VALUES (?, ?)";
        System.out.println("TYPE STMT: " + createType);
        try {
            CQLSSTableWriter writer = CQLSSTableWriter.builder()
                .inDirectory(dir).forTable(createTable).withType(createType)
                .using(insert).build();
            System.out.println("BUILD_OK: CQLSSTableWriter constructed without exception");
            writer.close();
        } catch (Throwable t) {
            System.out.println("BUILD_FAILED with: " + t.getClass().getName() + ": " + t.getMessage());
            t.printStackTrace(System.out);
            throw t;
        }
    }
}

--- Build & run (against the buggy 3.11.x JARs in /casslib) ---
javac -cp "/casslib/*" Repro.java
java -cp "/casslib/*:." Repro

--- Expected buggy result (exit code 1) ---
BUILD_FAILED with: org.apache.cassandra.exceptions.InvalidRequestException: \
  Non-frozen tuples are not allowed inside collections: list<tuple<text, text>>
(thrown from CQL3Type$Raw$RawCollection.throwNestedNonFrozenError via
 Types$RawBuilder$RawUDT.prepare -> CQLSSTableWriter$Builder.createTypes -> build)

--- Fixed result (3.11.8, exit code 0) ---
BUILD_OK: CQLSSTableWriter constructed without exception
"""

    # No CQL drives this fault, so the continuous-reproducer CQL-loop pod cannot trigger
    # it. Left False deliberately; do NOT flip it on (it would deploy a no-op CQL loop).
    continuous_reproducer = False
