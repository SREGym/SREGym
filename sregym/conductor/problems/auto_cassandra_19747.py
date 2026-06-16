"""CASSANDRA-19747: Invalid schema.cql created by snapshot after dropping more than one field.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-19747
Buggy: 4.1.5   Fixed: 4.1.6 (also 4.0.14, 5.0-rc1, 6.0-alpha1, 6.0)

Reproduction (single node, nodetool snapshot sequence):
  1. CREATE TABLE testtable (field1 PK, field2, field3); then ALTER TABLE DROP (field2, field3).
  2. nodetool snapshot -t my_snapshot <ks> — this writes the table's schema.cql into the snapshot dir.
  3. Read the snapshot's schema.cql. On 4.1.5 the comma between the two reconstructed dropped-column
     definitions is MISSING, producing invalid CQL that fails to parse on restore (SyntaxException).
     On 4.1.6 the comma is present and the schema round-trips.

This is NOT a plain CQL query result: the malformed artifact is the on-disk schema.cql produced by
`nodetool snapshot`, so it is encoded as a nodetool-sequence (diagnosis-only) problem with a custom
inject_fault() that runs the CQL + nodetool steps on the server pod and surfaces the comma-less
schema.cql in the inject log (mirrors the cassandra_20108.py kubectl-exec pattern). It is single-node:
the snapshot schema is generated locally on whichever node runs nodetool snapshot, so no multi-node
orchestration is needed.

VERBATIM BUGGY SIGNATURE (schema.cql is missing the comma after `field2 text`):

    field2 text
    field3 text

Root cause: src/java/org/apache/cassandra/schema/TableMetadata.java — appendColumnDefinitions().
For snapshots, includeDroppedColumns=true reconstructs the dropped columns (field2, field3) in a
second loop. The comma guard for each reconstructed dropped column is
`if (!hasSingleColumnPrimaryKey || iter.hasNext()) builder.append(',')`, but `iter` is the ALREADY
EXHAUSTED live-column iterator, not the dropped-column iterator. With a single-column PRIMARY KEY
(hasSingleColumnPrimaryKey=true) and the live iterator spent, the guard is false, so no comma is
appended between successive dropped-column definitions — the fix tests iterDropped.hasNext() instead.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra19747(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.5"
    source_git_ref = "cassandra-4.1.5"
    # 4.1.5 already ships the bug (fix landed in 4.1.6), so deploy the STOCK image
    # instead of running a ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/schema/TableMetadata.java"
    root_cause_description = (
        "The schema.cql emitted by `nodetool snapshot` is invalid after dropping two or more columns: "
        "the comma between the reconstructed dropped-column definitions is missing, so the CREATE TABLE "
        "in the snapshot fails to parse on restore (SyntaxException). The root cause is in "
        "TableMetadata.appendColumnDefinitions(): when includeDroppedColumns=true the dropped columns "
        "(field2, field3) are re-emitted in a second loop, but the comma guard "
        "`if (!hasSingleColumnPrimaryKey || iter.hasNext())` tests the ALREADY-EXHAUSTED live-column "
        "iterator `iter` instead of the dropped-column iterator. With a single-column PRIMARY KEY and "
        "the live iterator spent, the guard is false and no comma is appended between successive dropped "
        "columns. The fix tests iterDropped.hasNext() so each non-final dropped column gets its comma."
    )

    # Diagnosis-only nodetool sequence. The malformed artifact is the on-disk schema.cql produced by
    # `nodetool snapshot`, which the standard CQL-only continuous-reproducer pod (cqlsh < run.cql)
    # cannot run nor read off the server pod's disk — so continuous_reproducer stays False and there is
    # NO expected_output (no mitigation oracle). inject_fault() below runs the verified steps directly.
    continuous_reproducer = False

    # Verified buggy steps from the reproduction evidence log (kept as documentation; the live steps are
    # executed in inject_fault below). CQL statements are semicolon-terminated; the nodetool snapshot and
    # the schema.cql read are nodetool / shell steps, not CQL.
    reproducer = """
-- 1. Schema + multi-column drop (run via cqlsh on the server pod):
CREATE KEYSPACE IF NOT EXISTS repro19747 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '1'};
CREATE TABLE IF NOT EXISTS repro19747.testtable (field1 text PRIMARY KEY, field2 text, field3 text);
ALTER TABLE repro19747.testtable DROP (field2, field3);

-- 2. Snapshot the keyspace (nodetool, NOT CQL):
--    nodetool snapshot -t my_snapshot repro19747

-- 3. Read the snapshot's schema.cql (shell, NOT CQL) and observe the missing comma:
--    find /var/lib/cassandra/data/repro19747 -name schema.cql -exec cat {} +
--    BUGGY 4.1.5 output (note no comma after `field2 text`):
--        CREATE TABLE IF NOT EXISTS repro19747.testtable (
--            field1 text PRIMARY KEY,
--            field2 text
--            field3 text
--        ) WITH ...;
--        ALTER TABLE repro19747.testtable DROP field2 USING TIMESTAMP ...;
--        ALTER TABLE repro19747.testtable DROP field3 USING TIMESTAMP ...;
"""

    # CQL portion only — fed to cqlsh on the server pod before the nodetool/snapshot steps.
    _SETUP_CQL = (
        "CREATE KEYSPACE IF NOT EXISTS repro19747 WITH replication = "
        "{'class': 'SimpleStrategy', 'replication_factor': '1'}; "
        "CREATE TABLE IF NOT EXISTS repro19747.testtable "
        "(field1 text PRIMARY KEY, field2 text, field3 text); "
        "ALTER TABLE repro19747.testtable DROP (field2, field3);"
    )

    def _server_pod(self) -> str | None:
        """Return the name of a Cassandra server pod in this cluster's namespace."""
        result = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip().strip("'") or None

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, then run the verified nodetool-snapshot sequence on the
        server pod so the comma-less schema.cql is generated and surfaced in the inject log.

        Diagnosis-only: the bug manifests as a malformed on-disk schema.cql (invalid CQL that
        breaks snapshot restore), observed here rather than by a continuous reproducer pod.
        """
        if self._predeployed_buggy:
            logger.info("[AutoCassandra19747] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra19747] Injecting fault: swapping cluster to {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra19747] Buggy image active")

        self.setup_preconditions()

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra19747] No Cassandra server pod found — cannot run reproducer")
            return

        # 1. Create the schema and drop the two columns.
        logger.info("[AutoCassandra19747] Creating schema and dropping field2, field3")
        cql = self._SETUP_CQL.replace('"', '\\"')
        subprocess.run(
            f'kubectl exec -n {self.namespace} {pod} -c cassandra -- cqlsh {self._cqlsh_auth_flags()}-e "{cql}"',
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # 2. Snapshot the keyspace (this is the action that writes the malformed schema.cql).
        logger.info("[AutoCassandra19747] Taking nodetool snapshot (generates schema.cql)")
        subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- nodetool snapshot -t my_snapshot repro19747",
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # 3. Read the snapshot's schema.cql — the comma-less column defs appear here (buggy signature).
        logger.info("[AutoCassandra19747] Reading snapshot schema.cql (expect missing comma after 'field2 text')")
        schema = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"sh -c 'find /var/lib/cassandra/data/repro19747 -name schema.cql -exec cat {{}} +'",
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        logger.info(f"[AutoCassandra19747] Generated schema.cql:\n{schema.stdout}")
