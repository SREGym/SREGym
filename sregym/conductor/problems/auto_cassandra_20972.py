"""CASSANDRA-20972: SELECT DISTINCT over a flushed range tombstone throws IllegalStateException.

Title: SELECT DISTINCT + range tombstone IllegalStateException (UnfilteredRowIterator not closed).
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-20972

Buggy: 5.0.5   ->   Fixed: 5.0.6

Reproduction (single node, requires a `nodetool flush` so the data lands in an SSTable):
  1. Create keyspace k (RF=1) and table k.tbl with PRIMARY KEY (id, ck).
  2. Write a range tombstone (DELETE ... WHERE id=1 AND ck<10) then INSERT a live row (id=1, ck=5).
  3. `nodetool flush k tbl` — forces the range tombstone + row into a Big-format SSTable.
  4. SELECT DISTINCT id FROM k.tbl WHERE token(id) > -9223372036854775808 — fails on the buggy build.

On 5.0.5 the SELECT DISTINCT path over the SSTable reuses the partition's UnfilteredRowIterator
without closing it, so the next hasNext()/next() on the BigTableScanner trips an internal guard.

VERBATIM BUGGY SIGNATURE (5.0.5):
  ReadFailure; server log: IllegalStateException: The UnfilteredRowIterator ... must be closed
  before calling hasNext() or next() again  at SSTableScanner.java:241
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra20972(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.5"
    source_git_ref = "cassandra-5.0.5"
    # 5.0.5 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/io/sstable/format/big/BigTableScanner.java"
    root_cause_description = (
        "SELECT DISTINCT over an SSTable that contains a range tombstone followed by a live row "
        "throws an IllegalStateException ('The UnfilteredRowIterator ... must be closed before "
        "calling hasNext() or next() again' at SSTableScanner.java:241), surfaced to the client as "
        "a ReadFailure. On the DISTINCT + range-tombstone read path the per-partition "
        "UnfilteredRowIterator returned by the BigTableScanner is advanced again before the "
        "previous iterator was closed, so the scanner's not-yet-closed guard fires."
    )

    # The flushed SSTable is what triggers the bug, so the writes + `nodetool flush` are done once in
    # setup_preconditions() (below). The continuous reproducer / mitigation probe only needs to re-run
    # the failing SELECT against the already-flushed data: it errors on 5.0.5 and succeeds on 5.0.6.
    reproducer = """
SELECT DISTINCT id FROM k.tbl WHERE token(id) > -9223372036854775808;
"""
    continuous_reproducer = True
    # Error/ReadFailure bug (not a wrong-result bug): leave expected_output unset so the mitigation
    # oracle uses expect_unready=False (NotReady = bug present, Ready = fixed).

    # CQL that establishes the range-tombstone-then-live-row state. Run while the buggy image is
    # active, before the flush. Uses fixed timestamps so the live INSERT shadows the tombstone.
    _SETUP_CQL = """
CREATE KEYSPACE IF NOT EXISTS k WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
CREATE TABLE IF NOT EXISTS k.tbl (id int, ck int, x int, PRIMARY KEY (id, ck));
DELETE FROM k.tbl USING TIMESTAMP 100 WHERE id = 1 AND ck < 10;
INSERT INTO k.tbl (id, ck, x) VALUES (1, 5, 7) USING TIMESTAMP 101;
"""

    def setup_preconditions(self):
        """Write the range tombstone + live row, then flush so the data lands in an SSTable.

        The bug lives in the SSTable scanner, so the rows must be flushed out of the memtable
        before the SELECT DISTINCT can hit BigTableScanner. This runs once, after the buggy image
        is active and before the (looping) reproducer SELECT.
        """
        logger.info("[AutoCassandra20972] Writing range-tombstone + live-row state")
        try:
            self.app.run_reproducer(self._SETUP_CQL)
        except Exception as e:
            logger.warning(f"[AutoCassandra20972] setup CQL raised (unexpected): {e}")

        self._flush_keyspace_table("k", "tbl")

    def _flush_keyspace_table(self, keyspace: str, table: str):
        """Run `nodetool flush <ks> <tbl>` on every Cassandra server pod in the cluster.

        With RF=1 over a 3-node datacenter the partition lives on exactly one node and we don't
        know which, so flush all of them — a flush on a non-owning node is a harmless no-op. The
        K8ssandra/GenericDBApplication deploy path labels pods with
        app.kubernetes.io/instance={cluster_name} (see generic_db_app.py), so select on that label.
        """
        cluster = self.app.cluster_name
        pods_out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} "
                f"-l app.kubernetes.io/instance=cassandra-{cluster} "
                f"-o jsonpath='{{.items[*].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )

        pods = [p for p in pods_out.split() if p]
        if not pods:
            logger.warning(
                f"[AutoCassandra20972] No Cassandra pods found for cluster {cluster!r} in "
                f"{self.namespace!r} — skipping nodetool flush (SELECT may not reproduce the bug)"
            )
            return

        logger.info(f"[AutoCassandra20972] Flushing {keyspace}.{table} on {len(pods)} pod(s)")
        for pod in pods:
            result = subprocess.run(
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- nodetool flush {keyspace} {table}",
                shell=True,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.info(
                    f"[AutoCassandra20972] nodetool flush on {pod} exited "
                    f"{result.returncode}: {result.stderr.strip()[:200]}"
                )
