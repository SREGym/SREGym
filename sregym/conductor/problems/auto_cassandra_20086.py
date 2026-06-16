"""SAI vector search returns stale / wrong-ranked rows after a vector overwrite.

Title:   Use similarity score ordered iterators in SAI vector search; fix handling
         of updated vectors.
JIRA:    https://issues.apache.org/jira/browse/CASSANDRA-20086
Buggy:   5.0.6   ->   Fixed: 5.0.7 (also 6.0)
Component: Feature/Vector Search (Storage-Attached Indexing, ANN / vector<float,N>)

Reproduction summary (single node, RF=1):
  1. CREATE TABLE t (pk int PRIMARY KEY, val vector<float, 2>) + a StorageAttachedIndex on val.
  2. INSERT pk0=[1,2] and pk1=[1,3], then `nodetool flush` so those vectors land in
     SSTable A whose SAI index holds pk0 at the exact-match position [1,2].
  3. INSERT pk0=[1,4] (overwrite pk0 to a WORSE position; lives in the memtable / a newer SSTable).
  4. SELECT pk FROM t ORDER BY val ANN OF [1.0, 2.0] LIMIT 1.
Current stored values are pk0=[1,4], pk1=[1,3]; query vector is [1,2]. Euclidean distance
d(pk1)=1 < d(pk0)=2, so the correct nearest neighbour is pk1 (pk=1). The buggy 5.0.6 build
returns pk=0 because SSTable A's SAI index still ranks pk0's stale exact-match [1,2] best and
the row is materialised / returned without re-scoring against pk0's current value [1,4].

Verbatim buggy signature (from the reproduction evidence log):
  `SELECT pk FROM t ORDER BY val ann of [1.0, 2.0] LIMIT 1` returns `pk=0` on 5.0.6, but the
  correct nearest neighbor to [1,2] is pk=1 (pk1=[1,3] dist 1 < pk0=[1,4] dist 2). The Jira
  test asserts `row(1)`. The bug is wrong-ranked rows from a stale overwritten vector still in
  an SSTable's SAI index.

This is a WRONG-RESULT bug: the query returns an incorrect row (no exception). Per the SREGym
skill, ``expected_output`` is set to the BUGGY value (pk=0) so the reproducer-pod readiness probe
greps for it: Ready = the wrong/stale ANN result is present (bug active), NotReady = fixed.

Reproduction shape: nodetool-sequence. The stale-SSTable state requires a `nodetool flush`
*between* the seed inserts and the overwrite, which the CQL-only continuous probe cannot perform.
That state is established ONCE in ``setup_preconditions()`` (CREATE/INSERT/flush/overwrite on the
Cassandra server pod via ``kubectl exec``) while the buggy image is already active; the looped
``reproducer`` is just the final ANN SELECT, which reads the static on-disk state every cycle.
"""

import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra20086(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.6"
    source_git_ref = "cassandra-5.0.6"
    # 5.0.6 already ships the bug (fixed in 5.0.7), so deploy the stock image instead of
    # running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/index/sai/disk/vector"
    root_cause_description = (
        "SAI vector (ANN) search over a vector<float, N> column returns stale, wrong-ranked rows "
        "after a vector is overwritten. When an older value (pk0=[1,2]) has been flushed into an "
        "SSTable, that SSTable's SAI index still holds pk0 at its original exact-match position and "
        "ranks it best for a nearby query vector. After pk0 is updated to a worse position "
        "(pk0=[1,4], in a newer memtable/SSTable), the index-ordered top-k still surfaces the stale "
        "SSTable entry and the row is materialised/returned without re-scoring against pk0's current "
        "value, so `ORDER BY val ANN OF [1.0,2.0] LIMIT 1` returns pk=0 instead of the true nearest "
        "neighbour pk=1 (pk1=[1,3] dist 1 < pk0=[1,4] dist 2). The fix uses similarity-score-ordered "
        "iterators and re-scores updated vectors so overwritten rows are ranked by their current value."
    )

    # Looped probe query only — the flush-bearing setup is done once in setup_preconditions().
    # Fully qualified (cqlsh starts with no keyspace). Buggy 5.0.6 -> pk=0; fixed 5.0.7 -> pk=1.
    reproducer = """
SELECT pk FROM ks_clean20086.t ORDER BY val ANN OF [1.0, 2.0] LIMIT 1;
"""
    continuous_reproducer = True
    # WRONG-RESULT bug: the BUGGY value the query returns. The readiness probe greps for it, so
    # Ready = buggy/stale ANN result present (bug active), NotReady = fixed (returns pk=1).
    expected_output = "0"

    # ── Flush-bearing precondition state (the "nodetool flush" sequence) ───────────────────────
    # Runs during inject_fault() AFTER the buggy image is active and BEFORE the looped reproducer.
    _KEYSPACE = "ks_clean20086"
    _SETUP_CQL_1 = (
        "CREATE KEYSPACE IF NOT EXISTS ks_clean20086 "
        "WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};"
        " USE ks_clean20086;"
        " CREATE TABLE IF NOT EXISTS t (pk int PRIMARY KEY, val vector<float, 2>);"
        " CREATE CUSTOM INDEX IF NOT EXISTS ann_idx ON t(val)"
        " USING 'StorageAttachedIndex';"
        " INSERT INTO t (pk, val) VALUES (0, [1.0, 2.0]);"
        " INSERT INTO t (pk, val) VALUES (1, [1.0, 3.0]);"
    )
    # After FLUSH #1, SSTable A's SAI index holds pk0 at exact-match [1,2] and pk1 at [1,3].
    _SETUP_CQL_2_OVERWRITE = (
        "USE ks_clean20086;"
        " INSERT INTO t (pk, val) VALUES (0, [1.0, 4.0]);"  # overwrite pk0 to a WORSE position
    )

    def _server_pods(self) -> list[str]:
        """Return ALL Running Cassandra server pods (cass-operator manages them)."""
        out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} "
                f"-l app.kubernetes.io/name=cassandra "
                f"--field-selector=status.phase=Running "
                f"-o jsonpath='{{.items[*].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return [p for p in out.split() if p]

    def _superuser_creds(self) -> tuple[str, str]:
        """Read the K8ssandra-managed superuser credentials from the cluster secret."""
        import base64

        secret = f"{self.app.cluster_name}-superuser"
        u = (
            subprocess.run(
                f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.username}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        p = (
            subprocess.run(
                f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.password}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return (
            base64.b64decode(u).decode() if u else "cassandra",
            base64.b64decode(p).decode() if p else "cassandra",
        )

    def _exec_cql(self, pod: str, u_b64: str, p_b64: str, cql: str) -> None:
        subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f'cqlsh -u "$U" -p "$P" --request-timeout=60'
            f"'",
            shell=True,
            capture_output=True,
            text=True,
            input=cql,
            timeout=180,
        )

    def _exec_flush_all(self, pods: list[str], u_b64: str, p_b64: str) -> None:
        # nodetool flush is NODE-LOCAL. The cluster is 3 nodes at RF=1, so pk0 lives on exactly
        # one (unknown) replica — flush EVERY node so pk0's owner is guaranteed to persist its
        # stale [1,2] into an SSTable's SAI index. cass-management-api nodetool needs the JMX
        # superuser creds; pass them best-effort with a no-auth fallback.
        for pod in pods:
            subprocess.run(
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
                f"bash -c '"
                f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
                f'nodetool -u "$U" -pw "$P" flush {self._KEYSPACE} t || nodetool flush {self._KEYSPACE} t'
                f"'",
                shell=True,
                capture_output=True,
                text=True,
                timeout=180,
            )

    def setup_preconditions(self):
        """Establish the stale-SSTable state ONCE on the Cassandra server node:

            CREATE TABLE/INDEX + INSERT pk0=[1,2],pk1=[1,3]  -> nodetool flush (SSTable A)
            -> INSERT pk0=[1,4] (overwrite to a worse position).

        This must run on the server pod (so `nodetool flush` is local) while the buggy image is
        active, so the buggy build itself writes SSTable A's SAI index. The looped reproducer
        (the ANN SELECT) then reads this static on-disk state every cycle.
        """
        import base64

        pods = self._server_pods()
        if not pods:
            logger.warning("[AutoCassandra20086] No running Cassandra server pod found — skipping preconditions")
            return
        coordinator = pods[0]  # any node can coordinate the INSERTs; the operator handles routing
        try:
            username, password = self._superuser_creds()
            u_b64 = base64.b64encode(username.encode()).decode()
            p_b64 = base64.b64encode(password.encode()).decode()

            logger.info("[AutoCassandra20086] Seeding pk0=[1,2], pk1=[1,3] + SAI index")
            self._exec_cql(coordinator, u_b64, p_b64, self._SETUP_CQL_1)
            # Let the schema/index settle before flushing.
            time.sleep(3)
            logger.info(
                f"[AutoCassandra20086] nodetool flush on all {len(pods)} node(s) "
                f"-> stale vectors land in SSTable A's SAI index"
            )
            self._exec_flush_all(pods, u_b64, p_b64)
            time.sleep(2)
            logger.info("[AutoCassandra20086] Overwriting pk0 -> [1,4] (worse position)")
            self._exec_cql(coordinator, u_b64, p_b64, self._SETUP_CQL_2_OVERWRITE)
        except Exception as e:  # tolerate exec hiccups; base class is intentionally lenient here
            logger.warning(f"[AutoCassandra20086] setup_preconditions raised (continuing): {e}")
