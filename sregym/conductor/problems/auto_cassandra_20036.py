"""CASSANDRA-20036: snapshot recreate CQL omits the CREATE TYPE for a UDT used as a reverse clustering key.

Title: When snapshotting, the recreate CQL does not count UDTs from reverse clustering columns.
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-20036
Buggy: 5.0.2   Fixed: 4.0.15, 4.1.8, 5.0.3, 6.0-alpha1, 6.0

Reproduction summary:
  Create a frozen UDT and use it as a clustering key with CLUSTERING ORDER BY (ck DESC), so the
  column type is wrapped in ReverseType. Run `nodetool flush` + `nodetool snapshot`. The snapshot's
  recreate script (schema.cql) jumps straight to CREATE TABLE and omits the required CREATE TYPE,
  because TableMetadata#getReferencedUserTypes does not unwrap ReverseType and therefore misses the
  UDT. Dropping the table+type and replaying the buggy schema.cql in the (still-existing) keyspace
  fails: the CREATE TABLE references a type that was never (re)created. The same table with ASC
  ordering correctly includes the CREATE TYPE, proving the omission is ReverseType(DESC)-specific.

Verbatim buggy signature (from replaying the buggy snapshot schema.cql after dropping the type):
  schema.cql:26:InvalidRequest: Error from server: code=2200 [Invalid query] message="Unknown type repro20036.foo"

Shape note (NOT a pure-CQL continuous reproducer):
  This bug lives in the local snapshot path. Reproducing it requires `nodetool flush` + `nodetool
  snapshot` on the server pod, reading the generated schema.cql off the server pod's filesystem (the
  snapshot directory carries a runtime-generated table UUID), dropping the type/table, then replaying
  that file with `cqlsh -f`. The framework's CQL-only reproducer/mitigation path (a separate
  cassandra:4.1 client pod that only pipes CQL into cqlsh) CANNOT take a snapshot, cannot read the
  server's snapshot directory, and cannot run `cqlsh -f schema.cql`. So this is encoded as the
  decision-tree "nodetool / flush sequence" shape: inject_fault() is overridden to drive the full
  sequence via kubectl-exec on the server pod, and continuous_reproducer is left False
  (diagnosis-only, mitigation_oracle = None), like the other server-side Cassandra bug problems.
  (The INSERT-literal rejection noted in the evidence log is a separate UserTypes.java manifestation
  and is deliberately NOT used here, because it never exercises getReferencedUserTypes.)
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KEYSPACE = "repro20036"
_SNAPSHOT_TAG = "snapB"


class AutoCassandra20036(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.2"
    source_git_ref = "cassandra-5.0.2"
    # 5.0.2 already ships the bug (fix landed in 5.0.3), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/schema/TableMetadata.java"
    root_cause_description = (
        "When snapshotting, the snapshot's recreate script (schema.cql) omits the CREATE TYPE for a "
        "frozen UDT that is used as a clustering key in DESC order. TableMetadata#getReferencedUserTypes "
        "iterates columns() and calls addUserTypes(c.type, types), but a DESC clustering column's type is "
        "wrapped in ReverseType, which is not unwrapped before UDT detection. The referenced UDT is missed, "
        "so the recreate script has no CREATE TYPE and is non-executable in a fresh keyspace (the CREATE "
        "TABLE references an unknown type)."
    )

    # Canonical buggy steps (documentation / human-readable record). The actual multi-step
    # orchestration — nodetool flush+snapshot, reading schema.cql off the server pod, dropping the
    # objects, and replaying with `cqlsh -f` — is driven by inject_fault() below, because none of it
    # is expressible as a CQL string piped into cqlsh.
    reproducer = """
-- Setup (run via cqlsh on the server pod):
CREATE KEYSPACE IF NOT EXISTS repro20036 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TYPE IF NOT EXISTS repro20036.foo (a int);
CREATE TABLE repro20036.tbl_desc (pk int, ck frozen<foo>, PRIMARY KEY(pk, ck)) WITH CLUSTERING ORDER BY (ck DESC);

-- Persist + snapshot (run via nodetool on the server pod):
--   nodetool flush repro20036
--   nodetool snapshot -t snapB repro20036

-- Simulate a fresh restore: drop the table and the type (keep the keyspace):
DROP TABLE repro20036.tbl_desc;
DROP TYPE repro20036.foo;

-- Replay the BUGGY snapshot recreate script (run via `cqlsh -f` on the server pod):
--   cqlsh -f /var/lib/cassandra/data/repro20036/tbl_desc-<uuid>/snapshots/snapB/schema.cql
-- Buggy result (the CREATE TYPE was dropped from schema.cql):
--   schema.cql:26:InvalidRequest: Error from server: code=2200 [Invalid query] message="Unknown type repro20036.foo"
"""
    # Server-side (nodetool/snapshot/filesystem) bug: there is NO pure-CQL probe the CQL-only
    # reproducer pod can run to detect it, so this is diagnosis-only. Setting continuous_reproducer
    # True here would deploy a mitigation pod that runs these steps as CQL (cqlsh would error on the
    # nodetool/-f lines and stay permanently NotReady), which is worse than no mitigation oracle.
    continuous_reproducer = False
    # No expected_output: this is an error / non-executable-recreate-script bug, not a wrong-result bug.

    # ── CQL/nodetool driven on the server pod ─────────────────────────────────

    _SETUP_CQL = (
        f"CREATE KEYSPACE IF NOT EXISTS {_KEYSPACE} "
        "WITH replication = {'class':'SimpleStrategy','replication_factor':1}; "
        f"CREATE TYPE IF NOT EXISTS {_KEYSPACE}.foo (a int); "
        f"CREATE TABLE IF NOT EXISTS {_KEYSPACE}.tbl_desc "
        "(pk int, ck frozen<foo>, PRIMARY KEY(pk, ck)) WITH CLUSTERING ORDER BY (ck DESC);"
    )

    # Simulate a fresh-restore state: drop the table and the type but keep the keyspace, so replaying
    # the snapshot's schema.cql must recreate BOTH the type and the table.
    _DROP_CQL = f"DROP TABLE IF EXISTS {_KEYSPACE}.tbl_desc; DROP TYPE IF EXISTS {_KEYSPACE}.foo;"

    def _server_pod(self) -> str | None:
        """Return the name of a running cass-operator-managed Cassandra server pod.

        The cluster is deployed by the K8ssandra/cass-operator (see _cassandra_cluster_manifest), which
        labels server pods with ``cassandra.datastax.com/cluster=<cluster_name>``. We pick the first
        such pod; the snapshot logic is purely local so any one server pod is sufficient.
        """
        cluster = self.app.cluster_name
        selector = f"cassandra.datastax.com/cluster={cluster}"
        out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} -l {selector} -o jsonpath='{{.items[0].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return out or None

    def _exec(self, pod: str, inner: str, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run ``inner`` inside the ``cassandra`` container of ``pod`` via ``bash -c``."""
        inner = self._authed_cqlsh(inner)
        cmd = f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -lc {subprocess.list2cmdline([inner])}"
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

    @mark_fault_injected
    def inject_fault(self):
        """Drive the CASSANDRA-20036 snapshot reproduction on the server pod.

        Steps (all on the buggy 5.0.2 server pod):
          1. Ensure the buggy image is active (no-op when prebuilt_from_stock pre-deployed it).
          2. Create keyspace + UDT + DESC-clustering table; nodetool flush; nodetool snapshot.
          3. Locate the generated snapshots/<tag>/schema.cql (UUID is runtime — glob it).
          4. Drop the table and the type (fresh-restore state), keep the keyspace.
          5. Replay schema.cql with `cqlsh -f` — the buggy script omits CREATE TYPE, so it fails with
             ``InvalidRequest ... Unknown type repro20036.foo``.
        """
        # 1. Make sure the buggy binary is the one running (lifecycle parity with the base class).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra20036] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra20036] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra20036] Buggy image active")

        self.setup_preconditions()

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra20036] No Cassandra server pod found — cannot run reproducer")
            return
        logger.info(f"[AutoCassandra20036] Using server pod {pod}")

        # 2. Schema + data persisted to an sstable, then snapshot.
        logger.info("[AutoCassandra20036] Creating keyspace/UDT/DESC table")
        self._exec(pod, f"cqlsh -e {subprocess.list2cmdline([self._SETUP_CQL])}")
        logger.info("[AutoCassandra20036] nodetool flush + snapshot")
        self._exec(pod, f"nodetool flush {_KEYSPACE}")
        self._exec(pod, f"nodetool snapshot -t {_SNAPSHOT_TAG} {_KEYSPACE}")

        # 3. Find the recreate script (the table dir name has a runtime UUID, so glob it).
        schema_glob = f"/var/lib/cassandra/data/{_KEYSPACE}/tbl_desc-*/snapshots/{_SNAPSHOT_TAG}/schema.cql"
        find = self._exec(pod, f"ls -1 {schema_glob} 2>/dev/null | head -n1")
        schema_path = find.stdout.strip()
        if not schema_path:
            logger.warning(f"[AutoCassandra20036] Could not locate snapshot schema.cql under {schema_glob}")
            return
        logger.info(f"[AutoCassandra20036] Snapshot recreate script: {schema_path}")

        # 4. Fresh-restore state: drop the table + type, keep the keyspace.
        logger.info("[AutoCassandra20036] Dropping table + type (keeping keyspace)")
        self._exec(pod, f"cqlsh -e {subprocess.list2cmdline([self._DROP_CQL])}")

        # 5. Replay the buggy recreate script — expect the verbatim signature.
        logger.info("[AutoCassandra20036] Replaying buggy snapshot schema.cql (expect failure)")
        replay = self._exec(pod, f"cqlsh -f {schema_path}")
        combined = (replay.stdout + replay.stderr).strip()
        if "Unknown type" in combined:
            logger.info(f"[AutoCassandra20036] Reproduced (recreate script non-executable): {combined[:300]}")
        else:
            logger.warning(
                f"[AutoCassandra20036] Replay did not produce the expected 'Unknown type' error "
                f"(rc={replay.returncode}): {combined[:300]}"
            )
