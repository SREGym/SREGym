"""Reproduces CASSANDRA-18105 where truncated data reappears after a node restart. When a secondary index is created on a table, its internal table ID is shared with the base table. Dropping the index after a TRUNCATE removes the truncation record from system.local's truncated_at map for the base table. On restart, commitlog replay has no truncation record and replays pre-truncate mutations, causing deleted rows to resurface. Affected versions include 2.2.19, 3.0.28, 3.11.14, 4.0.9, and 4.1.1. The root cause is in ColumnFamilyStore where removeTruncationRecord is called unconditionally during index drop.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-18105
"""

import base64 as _b64
import logging
import subprocess

from sregym.conductor.problems.cassandra_bug import CassandraBugProblem
from sregym.service.apps.cassandra import Cassandra
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_TRIGGER_CQL = """
        CREATE KEYSPACE IF NOT EXISTS ks WITH REPLICATION = { 'class' : 'SimpleStrategy', 'replication_factor' : 1 }; CREATE TABLE ks.tb (c3 TEXT, c4 TEXT, c2 INT, c1 TEXT, PRIMARY KEY (c1, c2, c3)); INSERT INTO ks.tb (c3, c1, c2) VALUES ('val1','val2',1); CREATE INDEX IF NOT EXISTS tb ON ks.tb (c3); TRUNCATE TABLE ks.tb; DROP INDEX IF EXISTS ks.tb; SELECT c2 FROM ks.tb;
"""

_FAILING_SELECT = "SELECT c2 FROM ks.tb;"

class Cassandra18105(CassandraBugProblem):
    cassandra_version = "4.1.1"
    source_git_ref = "cassandra-4.1.1"

    root_cause_file = "src/java/org/apache/cassandra/db/ColumnFamilyStore.java"
    root_cause_description = (
        "When dropping a secondary index, ColumnFamilyStore calls SystemKeyspace.removeTruncationRecord with the index's table metadata ID, which is the same as the base table's ID (shared via CassandraIndex). This removes the truncation record for the base table from system.local's truncated_at map. On restart, commitlog replay finds no truncation record for the base table and replays pre-truncate mutations, resurrecting deleted data. The fix adds a check to skip removing the truncation record when the metadata belongs to an index."
    )

    trigger_cql = _TRIGGER_CQL

    @mark_fault_injected
    def inject_fault(self):
        """Set up the data state then start a background loop that keeps firing
        the failing query so AssertionError appears continuously in logs.
        """
        logger.info("[Cassandra18105] Running setup CQL")
        try:
            self.app.run_cql(self.trigger_cql)
        except Exception as e:
            logger.info(f"[Cassandra18105] Setup CQL error (may be expected): {e}")

        logger.info("[Cassandra18105] Firing initial failing query")
        try:
            self.app.run_cql(_FAILING_SELECT)
        except Exception as e:
            logger.info(f"[Cassandra18105] Expected AssertionError: {e}")

        logger.info("[Cassandra18105] Starting background query loop")
        self._start_background_workload()

    def _start_background_workload(self):
        """Fire the failing SELECT every 15 s so Cassandra18105 keeps appearing in logs."""
        pod = subprocess.run(
            f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")

        if not pod:
            logger.warning("[Cassandra18105] No Cassandra pod found — skipping background workload")
            return

        username, password = self.app._get_cql_credentials()
        u_b64 = _b64.b64encode(username.encode()).decode()
        p_b64 = _b64.b64encode(password.encode()).decode()

        cmd = (
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f"while true; do "
            f"cqlsh -u \"$U\" -p \"$P\" -e \"{_FAILING_SELECT}\" 2>&1; "
            f"sleep 15; "
            f"done'"
        )
        self._workload_proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"[Cassandra18105] Background workload started on pod {pod}")

    @mark_fault_injected
    def recover_fault(self):
        """Stop the background query loop."""
        proc = getattr(self, "_workload_proc", None)
        if proc is not None:
            proc.terminate()
            self._workload_proc = None
            logger.info("[Cassandra18105] Background workload stopped")
