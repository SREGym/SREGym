"""CASSANDRA-13935: Indexes (and UDTs) creation should have IF NOT EXISTS on its String representation.

JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-13935
Buggy: 3.11.8   Fixed: 3.11.9 (also 3.0.23, 4.0-beta3, 4.0)

Reproduction summary (single node, nodetool snapshot sequence):
  Create a keyspace + table + an AUTO-NAMED secondary index, then run `nodetool snapshot`.
  The snapshot writes a `schema.cql` recreate script: the CREATE TABLE is emitted with
  `IF NOT EXISTS`, but the accompanying CREATE INDEX is emitted WITHOUT `IF NOT EXISTS`.
  Replaying that generated file over the still-existing schema therefore "fails miserably"
  because the unguarded CREATE INDEX collides ("Index ... already exists") — a non-idempotent
  restore. The fixed build emits `CREATE INDEX IF NOT EXISTS ...` and the replay is idempotent.

This is NOT a plain CQL query result: the defect is the on-disk schema.cql recreate script produced
by `nodetool snapshot`, plus its non-idempotent replay. The standard CQL-only continuous-reproducer
pod (a separate cassandra:4.1 client that only pipes CQL into cqlsh) cannot run `nodetool snapshot`,
cannot read the server pod's snapshot directory, and cannot run `cqlsh -f schema.cql`. So this is
encoded as the decision-tree "nodetool / flush sequence" shape: inject_fault() is overridden to drive
the full sequence via kubectl-exec on the server pod, and continuous_reproducer is left False
(diagnosis-only, mitigation_oracle = None), like the other server-side Cassandra snapshot bug problems
(auto_cassandra_19747, auto_cassandra_20036). NB a bare `CREATE INDEX` run twice errors with the same
"already exists" text on BOTH the buggy and fixed builds, so the standalone proof is the generated-file
CREATE INDEX line itself (present vs `IF NOT EXISTS`-guarded), not the replay error in isolation.

VERBATIM BUGGY SIGNATURE (literal copy of the last line of the buggy 3.11.8 generated schema.cql):

    CREATE INDEX table1_last_update_date_idx ON repro13935.table1 (last_update_date);

(The CREATE TABLE line above it IS guarded — `CREATE TABLE IF NOT EXISTS repro13935.table1 (...` — so
the missing `IF NOT EXISTS` on the CREATE INDEX line is the sole A/B discriminator vs 3.11.9.)

Root cause: src/java/org/apache/cassandra/schema/IndexMetadata.java — the toCqlString() / String
representation used by the snapshot recreate-script generator emits `CREATE INDEX <name> ON ...` without
an `IF NOT EXISTS` clause (UDTs have the same omission), making the generated schema.cql non-idempotent
on replay. The fix adds `IF NOT EXISTS` to that emitted String representation.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KEYSPACE = "repro13935"
_SNAPSHOT_TAG = "snap1"
_INDEX_NAME = "table1_last_update_date_idx"


class AutoCassandra13935(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.8"
    source_git_ref = "cassandra-3.11.8"
    # 3.11.8 already ships the bug (fix landed in 3.11.9), so deploy the STOCK image
    # instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/schema/IndexMetadata.java"
    root_cause_description = (
        "The schema.cql recreate script emitted by `nodetool snapshot` is non-idempotent for tables "
        "that have a secondary index: the CREATE TABLE statement is written with `IF NOT EXISTS`, but "
        "the accompanying CREATE INDEX statement is written WITHOUT `IF NOT EXISTS`. Replaying the "
        "generated schema.cql over an existing (or partially restored) schema therefore fails with "
        "'Index <name> already exists', breaking snapshot restore. The defect is in the String "
        "representation that the snapshot generator uses for index DDL (IndexMetadata's CQL string "
        "output; UDTs share the same omission); the fix adds `IF NOT EXISTS` to that emitted statement."
    )

    # Verified buggy steps from the reproduction evidence log (kept as the human-readable record /
    # single source of truth). The actual orchestration — nodetool snapshot, reading schema.cql off the
    # server pod, asserting the unguarded CREATE INDEX line, then replaying with `cqlsh -f` — is driven
    # by inject_fault() below, because none of it is expressible as a CQL string piped into cqlsh.
    # CQL statements are semicolon-terminated; the nodetool snapshot, the schema.cql read, and the
    # `cqlsh -f` replay are nodetool / shell steps, not CQL.
    reproducer = """
-- 1. Schema + auto-named secondary index (run via cqlsh on the server pod):
CREATE KEYSPACE IF NOT EXISTS repro13935 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro13935.table1 (id text PRIMARY KEY, content text, last_update_date date, last_update_date_time timestamp);
CREATE INDEX ON repro13935.table1 (last_update_date);   -- auto-named => table1_last_update_date_idx

-- 2. Snapshot the keyspace (nodetool, NOT CQL) — this writes the schema.cql recreate script:
--    nodetool snapshot -t snap1 repro13935

-- 3. Read the snapshot's schema.cql (shell, NOT CQL). On 3.11.8 the CREATE INDEX line is UNGUARDED:
--    find /var/lib/cassandra/data/repro13935 -name schema.cql -exec cat {} +
--    BUGGY 3.11.8 (last line, note NO `IF NOT EXISTS`):
--        CREATE INDEX table1_last_update_date_idx ON repro13935.table1 (last_update_date);
--    FIXED 3.11.9 would instead emit:
--        CREATE INDEX IF NOT EXISTS table1_last_update_date_idx ON repro13935.table1 (last_update_date);

-- 4. Replay the generated schema.cql over the still-existing schema (run via `cqlsh -f` on the pod):
--    cqlsh -f /var/lib/cassandra/data/repro13935/table1-<uuid>/snapshots/snap1/schema.cql
--    BUGGY 3.11.8 result (the unguarded CREATE INDEX collides — non-idempotent restore):
--        InvalidRequest: Error from server: code=2200 [Invalid query] message="Index table1_last_update_date_idx already exists"
"""

    # Server-side (nodetool/snapshot/filesystem) bug: there is NO pure-CQL probe the CQL-only
    # reproducer pod can run to detect it (it cannot take a snapshot, read the server's snapshot
    # directory, or run `cqlsh -f schema.cql`), so this is diagnosis-only. Setting continuous_reproducer
    # True would deploy a mitigation pod that pipes these steps as CQL — cqlsh would error on the
    # nodetool/-f lines and stay permanently NotReady, a false mitigation signal — which is worse than
    # no mitigation oracle. The diagnosis LLMAsAJudgeOracle still works off root_cause_description.
    continuous_reproducer = False
    # No expected_output: this is an error / non-idempotent-recreate-script bug, not a wrong-result bug.

    # ── CQL/nodetool driven on the server pod ─────────────────────────────────

    # CQL portion only — fed to cqlsh on the server pod before the nodetool/snapshot steps. The index is
    # auto-named (no name given) so Cassandra derives `table1_last_update_date_idx`, matching the log.
    _SETUP_CQL = (
        f"CREATE KEYSPACE IF NOT EXISTS {_KEYSPACE} "
        "WITH replication = {'class':'SimpleStrategy','replication_factor':1}; "
        f"CREATE TABLE IF NOT EXISTS {_KEYSPACE}.table1 "
        "(id text PRIMARY KEY, content text, last_update_date date, last_update_date_time timestamp); "
        f"CREATE INDEX IF NOT EXISTS ON {_KEYSPACE}.table1 (last_update_date);"
    )

    def _server_pod(self) -> str | None:
        """Return the name of a running cass-operator-managed Cassandra server pod.

        The cluster is deployed by the K8ssandra/cass-operator (see _cassandra_cluster_manifest), which
        labels server pods with ``cassandra.datastax.com/cluster=<cluster_name>``. We pick the first
        such pod; the snapshot logic is purely local, so any one server pod is sufficient (single-node
        topology per the evidence log).
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
        """Run ``inner`` inside the ``cassandra`` container of ``pod`` via ``bash -lc``."""
        inner = self._authed_cqlsh(inner)
        cmd = f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -lc {subprocess.list2cmdline([inner])}"
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

    @mark_fault_injected
    def inject_fault(self):
        """Drive the CASSANDRA-13935 snapshot reproduction on the buggy 3.11.8 server pod.

        Steps (all on the server pod):
          1. Ensure the buggy image is active (no-op when prebuilt_from_stock pre-deployed it).
          2. Create keyspace + table + auto-named secondary index.
          3. nodetool snapshot — writes the snapshot's schema.cql recreate script.
          4. Locate the generated snapshots/<tag>/schema.cql (table dir UUID is runtime — glob it) and
             surface it; assert the buggy signature: a CREATE INDEX line WITHOUT `IF NOT EXISTS` (the
             literal artifact the fix changed — the standalone proof).
          5. Replay schema.cql with `cqlsh -f` over the still-existing schema — the unguarded CREATE
             INDEX collides with "Index table1_last_update_date_idx already exists" (corroborating
             impact: the non-idempotent restore the reporter hit).
          6. Leave a background loop re-running the replay so the error stays fresh in system.log / any
             time-windowed log tail (diagnosis-only: this loop is the only running manifestation).
        """
        # 1. Make sure the buggy binary is the one running (lifecycle parity with the base class).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra13935] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra13935] Injecting fault: swapping cluster to {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra13935] Buggy image active")

        self.setup_preconditions()

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra13935] No Cassandra server pod found — cannot run reproducer")
            return
        logger.info(f"[AutoCassandra13935] Using server pod {pod}")

        # 2. Schema + auto-named secondary index.
        logger.info("[AutoCassandra13935] Creating keyspace/table + auto-named secondary index")
        self._exec(pod, f"cqlsh -e {subprocess.list2cmdline([self._SETUP_CQL])}")

        # 3. Snapshot the keyspace (this is the action that writes the schema.cql recreate script).
        logger.info(f"[AutoCassandra13935] Taking nodetool snapshot -t {_SNAPSHOT_TAG} {_KEYSPACE}")
        self._exec(pod, f"nodetool snapshot -t {_SNAPSHOT_TAG} {_KEYSPACE}")

        # 4. Find the recreate script (the table dir name has a runtime UUID, so glob it) and surface it.
        schema_glob = f"/var/lib/cassandra/data/{_KEYSPACE}/table1-*/snapshots/{_SNAPSHOT_TAG}/schema.cql"
        find = self._exec(pod, f"ls -1 {schema_glob} 2>/dev/null | head -n1")
        schema_path = find.stdout.strip()
        if not schema_path:
            logger.warning(f"[AutoCassandra13935] Could not locate snapshot schema.cql under {schema_glob}")
            return
        logger.info(f"[AutoCassandra13935] Snapshot recreate script: {schema_path}")

        schema = self._exec(pod, f"cat {schema_path}")
        logger.info(f"[AutoCassandra13935] Generated schema.cql:\n{schema.stdout}")
        # Assert the PRIMARY EVIDENCE: a CREATE INDEX line that is NOT guarded by IF NOT EXISTS.
        index_lines = [ln for ln in schema.stdout.splitlines() if "CREATE INDEX" in ln.upper()]
        unguarded = [ln for ln in index_lines if "IF NOT EXISTS" not in ln.upper()]
        if unguarded:
            logger.info(
                "[AutoCassandra13935] Reproduced (snapshot emitted CREATE INDEX without IF NOT EXISTS): "
                + "; ".join(ln.strip() for ln in unguarded)
            )
        else:
            logger.warning(
                "[AutoCassandra13935] Did not find an unguarded CREATE INDEX line in schema.cql "
                f"(index lines: {index_lines!r}) — bug may not be present on this build"
            )

        # 5. Replay the generated schema.cql over the still-existing schema — expect the collision.
        logger.info("[AutoCassandra13935] Replaying generated schema.cql over existing schema (expect failure)")
        replay = self._exec(pod, f"cqlsh -f {schema_path}")
        combined = (replay.stdout + replay.stderr).strip()
        if "already exists" in combined.lower():
            logger.info(f"[AutoCassandra13935] Non-idempotent restore confirmed (replay failed): {combined[:300]}")
        else:
            logger.warning(
                f"[AutoCassandra13935] Replay did not produce the expected 'already exists' error "
                f"(rc={replay.returncode}): {combined[:300]}"
            )

        # 6. Background loop keeps re-firing the failing replay so the signal stays fresh in system.log.
        #    Each replay over the now-existing schema re-hits the unguarded CREATE INDEX collision.
        #    Mirrors the cassandra_20108.py / auto_cassandra_19880.py kubectl-exec background-loop
        #    pattern while keeping continuous_reproducer False (no cqlsh-based mitigation oracle).
        loop_cmd = (
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -lc {subprocess.list2cmdline([self._authed_cqlsh('while true; do cqlsh -f ' + schema_path + ' 2>&1; sleep 15; done')])}"
        )
        self._workload_proc = subprocess.Popen(
            loop_cmd,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(f"[AutoCassandra13935] Background replay loop started on pod {pod}")

    @mark_fault_injected
    def recover_fault(self):
        """Stop the background replay loop, then restore the stock image via the base-class recovery."""
        proc = getattr(self, "_workload_proc", None)
        if proc is not None:
            proc.terminate()
            self._workload_proc = None
            logger.info("[AutoCassandra13935] Background replay loop stopped")
        super().recover_fault()
