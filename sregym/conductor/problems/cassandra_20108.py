"""CASSANDRA-20108: IndexOutOfBoundsException when filtering on deleted column values.

Bug: When executing queries with ALLOW FILTERING on partitions where columns have been
deleted (tombstoned), Cassandra throws an IndexOutOfBoundsException. The root cause is
in RowFilter.java where getValue() returns cell.buffer() for tombstoned cells — these
have an empty ByteBuffer (size 0), causing an out-of-bounds access during comparison.

Affected versions: 3.0 through 4.0.15, 4.1.7, 5.0.2 (fixed in 4.0.16, 4.1.8, 5.0.3)
Fix: Check cell.isTombstone() || !cell.isLive(nowInSec) before accessing the buffer.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20108
Fix commit: 4fc8bb29fcda935728d8863a4499fa0e9d924b82
"""

import base64 as _b64
import logging
import subprocess

from sregym.conductor.problems.cassandra_bug import CassandraBugProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class Cassandra20108(CassandraBugProblem):
    # Use Cassandra 4.1.7 — last version before the fix (4.1.8)
    cassandra_version = "4.1.7"
    source_git_ref = "cassandra-4.1.7"
    allows_rebuild = True

    root_cause_file = "src/java/org/apache/cassandra/db/filter/RowFilter.java"
    root_cause_description = (
        "IndexOutOfBoundsException when filtering on deleted column values. "
        "In RowFilter.java, the getValue() method returns cell.buffer() for tombstoned cells "
        "without checking if the cell is a tombstone or expired. Tombstoned cells have an "
        "empty ByteBuffer (size 0), causing an IndexOutOfBoundsException in ByteType.compareCustom() "
        "when the buffer is compared against a filter value. The fix is to check "
        "cell.isTombstone() || !cell.isLive(nowInSec) before accessing the buffer."
    )

    # CQL sequence that triggers the bug:
    # 1. Create a table with static and regular columns
    # 2. Insert rows
    # 3. Delete specific columns (creating tombstones with empty ByteBuffers)
    # 4. Query with ALLOW FILTERING on the deleted columns → IndexOutOfBoundsException
    # Setup CQL: creates the schema and data state that exposes the bug.
    # The final SELECT is omitted here — inject_fault fires it in a loop.
    trigger_cql = """
        CREATE KEYSPACE IF NOT EXISTS test_ks
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};

        USE test_ks;

        CREATE TABLE IF NOT EXISTS filter_bug (
            pk int,
            ck int,
            s0 int static,
            v0 int,
            v1 int,
            PRIMARY KEY (pk, ck)
        );

        INSERT INTO filter_bug (pk, ck, s0, v0, v1) VALUES (0, 0, 0, 0, 0);
        INSERT INTO filter_bug (pk, ck, s0, v0, v1) VALUES (0, 1, 0, 1, 1);
        INSERT INTO filter_bug (pk, ck, s0, v0, v1) VALUES (1, 0, 1, 0, 0);

        DELETE s0, v0, v1 FROM filter_bug WHERE pk = 0 AND ck = 0;
    """

    # The SELECT that trips the bug — repeated in the background loop.
    _FAILING_SELECT = "SELECT * FROM test_ks.filter_bug WHERE v0 = 0 ALLOW FILTERING;"

    @mark_fault_injected
    def inject_fault(self):
        """Set up the tombstone state then start a background loop that keeps
        firing the failing SELECT so IndexOutOfBoundsException appears
        continuously in Cassandra's system log and is visible via Loki.
        """
        logger.info("[Cassandra20108] Running setup CQL to create tombstone state")
        try:
            self.app.run_cql(self.trigger_cql)
        except Exception as e:
            logger.info(f"[Cassandra20108] Setup CQL error (unexpected): {e}")

        # Fire once immediately so the exception appears before the loop starts
        logger.info("[Cassandra20108] Firing initial failing SELECT")
        try:
            self.app.run_cql(self._FAILING_SELECT)
        except Exception as e:
            logger.info(f"[Cassandra20108] Expected IndexOutOfBoundsException: {e}")

        logger.info("[Cassandra20108] Starting background query loop")
        self._start_background_workload()

    def _start_background_workload(self):
        """Fire the failing SELECT every 15 s so the IndexOutOfBoundsException
        keeps appearing in Cassandra's system log and Loki."""
        pod = subprocess.run(
            f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")

        if not pod:
            logger.warning("[Cassandra20108] No Cassandra pod found — skipping background workload")
            return

        username, password = self.app._get_cql_credentials()
        u_b64 = _b64.b64encode(username.encode()).decode()
        p_b64 = _b64.b64encode(password.encode()).decode()

        cmd = (
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f"while true; do "
            f"cqlsh -u \"$U\" -p \"$P\" -e \"{self._FAILING_SELECT}\" 2>&1; "
            f"sleep 15; "
            f"done'"
        )
        self._workload_proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"[Cassandra20108] Background workload started on pod {pod}")

    @mark_fault_injected
    def recover_fault(self):
        """Stop the background query loop."""
        proc = getattr(self, "_workload_proc", None)
        if proc is not None:
            proc.terminate()
            self._workload_proc = None
            logger.info("[Cassandra20108] Background workload stopped")
