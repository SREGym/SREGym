"""CASSANDRA-15134: SASI index files are not included in snapshots.

JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-15134
Buggy: 4.0.1   Fixed: 4.0.2 (also 3.11.12, 4.1-alpha1, 4.1)
Fix commit: 24b084fcf8ea64ccf117cd0e98310b1e1b40b6b8

Reproduction summary (config-gated, single node, nodetool flush + snapshot sequence):
  In ONE live session (no node restart between flush and snapshot — a restart re-scans the
  on-disk SASI files into the components list and MASKS the bug):
    1. Enable SASI in cassandra.yaml (enable_sasi_indexes: true) — SASI is gated off by
       default in 4.0. Done by post_deploy() via the K8ssandraCluster cassandraYaml block so
       the operator performs a rolling restart and the gate stays open across the image swap.
    2. CREATE keyspace + table + a SASI CUSTOM INDEX ('org.apache.cassandra.index.sasi.SASIIndex'),
       INSERT rows.
    3. `nodetool flush` — writes new sstables AND builds the per-sstable SASI index file
       (nb-*-big-SI_<index>.db) on disk.
    4. `nodetool snapshot` — hard-links the sstable's components into the snapshot directory.
  On buggy 4.0.1 the freshly-flushed sstable's SASI index file (SI_*.db) exists on disk but was
  never added to that sstable's components list, so `nodetool snapshot` does not hard-link it ->
  the SASI index file is MISSING from the snapshot.

This is NOT a CQL query result and NOT a query-time exception: the defect is a missing file in the
on-disk snapshot directory. The standard CQL-only continuous-reproducer pod (a separate cassandra:4.1
client that only pipes CQL into cqlsh) cannot run `nodetool flush`/`nodetool snapshot`, and the
buggy-vs-fixed difference (SI_*.db present/absent in the snapshot dir) never appears in cqlsh stdout
nor in pod readiness. So this is encoded as the decision-tree "nodetool / flush sequence" shape:
inject_fault() is overridden to drive the full config-gated flush+snapshot sequence via kubectl-exec
on the server pod(s), and continuous_reproducer is left False (diagnosis-only, mitigation_oracle =
None) — exactly like the other server-side Cassandra snapshot bug problems (auto_cassandra_13935,
auto_cassandra_19747, auto_cassandra_20036). Setting continuous_reproducer True would deploy a
mitigation pod that pipes these steps as CQL: cqlsh errors on the nodetool lines and stays
permanently NotReady (a false mitigation signal), which is worse than no mitigation oracle. The
diagnosis LLMAsAJudgeOracle still grades against root_cause_description below.

VERBATIM BUGGY SIGNATURE (from the reproduction evidence log, buggy 4.0.1):
  LIVE sstable dir contains the SASI index file:
      nb-1-big-SI_t_name_sasi.db
      nb-2-big-SI_t_name_sasi.db
  SNAPSHOT dir (snapshots/<tag>) copies Data.db/Index.db/Filter.db/etc. but contains NO *SI_* file:
      find snapshots/<tag> -name '*SI_*'   ->   (empty: SASI files MISSING from snapshot)
  and the live sstable's on-disk components list (TOC.txt) does NOT include the SI_ component:
      Data.db / Summary.db / Digest.crc32 / Filter.db / Statistics.db / Index.db / TOC.txt /
      CompressionInfo.db          (no SI_<index>.db line)
  (On fixed 4.0.2 the snapshot dir DOES include nb-1-big-SI_t_name_sasi.db and the live TOC.txt lists
  the SI_ component.)
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KEYSPACE = "repro15134_ks"
_TABLE = "t"
_INDEX_NAME = "t_name_sasi"
_SNAPSHOT_TAG = "repro15134_snap"


class AutoCassandra15134(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    # Buggy version = fix patch (4.0.2) - 1. The bug already ships in the stock 4.0.1 image, so
    # deploy/re-tag the stock image instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    # Verified against the fix commit (24b084f) and the buggy ref: the update() method of
    # DataTracker.java builds the SASI View but never registers the per-column index component on the
    # newly-indexed sstables. The fix adds, inside update()'s loop over the indexed sstables,
    # `sstable.addComponents(Collections.singleton(columnIndex.getComponent()))`, so the SI_<index>.db
    # component lands in SSTable#components and `nodetool snapshot` hard-links it.
    root_cause_file = "src/java/org/apache/cassandra/index/sasi/conf/DataTracker.java"
    root_cause_description = (
        "Newly written SASI index files are not included in snapshots. Per the Jira report, this is "
        "because the SASI index files are not added to the components (org.apache.cassandra.io.sstable."
        "SSTable#components) list of newly written sstables. When a memtable is flushed, the per-sstable "
        "SASI index file (nb-*-big-SI_<index>.db) is built on disk, but org.apache.cassandra.index.sasi."
        "conf.DataTracker.update() does not register that index component on the freshly-indexed "
        "SSTableReader, so the sstable's components list (and its TOC.txt) omits the SI_ component. "
        "`nodetool snapshot` only hard-links files listed in the sstable's components, so the SASI index "
        "file is left out of the snapshot directory. On startup Cassandra DOES re-scan on-disk SASI index "
        "files of existing sstables into their components list, so sstables that already existed at "
        "startup snapshot their SASI index correctly — only sstables flushed in the live session are "
        "affected (a restart between flush and snapshot masks the bug). The fix adds an "
        "addComponents(columnIndex.getComponent()) call when the index is built so the component is "
        "registered before any snapshot is taken."
    )

    # Verified buggy steps from the reproduction evidence log (kept as the human-readable record /
    # single source of truth). The actual orchestration — enable_sasi_indexes via the CR, the CQL
    # create/insert, nodetool flush, nodetool snapshot, and comparing the live sstable dir vs the
    # snapshot dir for the SI_*.db component — is driven by post_deploy() + inject_fault() below,
    # because none of it is expressible as a CQL string piped into cqlsh. CQL statements are
    # semicolon-terminated; the nodetool flush/snapshot and the directory listings are nodetool /
    # shell steps, not CQL.
    reproducer = """
-- Step 1 (cassandra.yaml, enabled by post_deploy via the operator — SASI is gated off by default in 4.0):
--   enable_sasi_indexes: true
-- Step 2 (CQL, run via cqlsh on the server pod):
CREATE KEYSPACE IF NOT EXISTS repro15134_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro15134_ks.t (id int PRIMARY KEY, name text);
CREATE CUSTOM INDEX t_name_sasi ON repro15134_ks.t (name) USING 'org.apache.cassandra.index.sasi.SASIIndex';
INSERT INTO repro15134_ks.t (id, name) VALUES (1, 'alpha');
INSERT INTO repro15134_ks.t (id, name) VALUES (2, 'beta');
INSERT INTO repro15134_ks.t (id, name) VALUES (3, 'gamma');
-- Step 3 (nodetool, NOT CQL — writes new sstables + builds the per-sstable SI_<index>.db on disk):
--   nodetool flush repro15134_ks
-- Step 4 (nodetool, NOT CQL — hard-links the sstable components into the snapshot dir):
--   nodetool snapshot -t repro15134_snap -kt repro15134_ks.t
-- Step 5 (shell, NOT CQL — compare the live sstable dir vs the snapshot dir):
--   live dir:     ls .../repro15134_ks/t-<uuid>/                 -> contains nb-*-big-SI_t_name_sasi.db
--   snapshot dir: ls .../t-<uuid>/snapshots/repro15134_snap/     -> NO *SI_* file (BUGGY 4.0.1)
--   find .../snapshots/repro15134_snap -name '*SI_*'             -> (empty on buggy build)
-- On fixed 4.0.2 the snapshot dir DOES include nb-*-big-SI_t_name_sasi.db.
"""

    # Server-side (nodetool/snapshot/filesystem) bug: there is NO pure-CQL probe the CQL-only
    # reproducer pod can run to detect it (it cannot flush, take a snapshot, or read the server's
    # snapshot directory), so this is diagnosis-only. Setting continuous_reproducer True would deploy a
    # mitigation pod that pipes these steps as CQL — cqlsh would error on the nodetool/shell lines and
    # stay permanently NotReady, a false mitigation signal — which is worse than no mitigation oracle
    # (see auto_cassandra_13935 / 19747 / 20036, the same server-side snapshot shape). The diagnosis
    # LLMAsAJudgeOracle still works off root_cause_description.
    continuous_reproducer = False
    # No expected_output: this is a missing-snapshot-file bug, not a wrong-result bug.

    # ── CQL/nodetool driven on the server pod ─────────────────────────────────

    # CQL portion only — fed to cqlsh on the server pod before the nodetool flush/snapshot steps.
    _SETUP_CQL = (
        f"CREATE KEYSPACE IF NOT EXISTS {_KEYSPACE} "
        "WITH replication = {'class':'SimpleStrategy','replication_factor':1}; "
        f"CREATE TABLE IF NOT EXISTS {_KEYSPACE}.{_TABLE} (id int PRIMARY KEY, name text); "
        f"CREATE CUSTOM INDEX IF NOT EXISTS {_INDEX_NAME} ON {_KEYSPACE}.{_TABLE} (name) "
        "USING 'org.apache.cassandra.index.sasi.SASIIndex'; "
        f"INSERT INTO {_KEYSPACE}.{_TABLE} (id, name) VALUES (1, 'alpha'); "
        f"INSERT INTO {_KEYSPACE}.{_TABLE} (id, name) VALUES (2, 'beta'); "
        f"INSERT INTO {_KEYSPACE}.{_TABLE} (id, name) VALUES (3, 'gamma');"
    )

    def post_deploy(self):
        """Enable SASI indexes on the deployed cluster before the reproducer runs.

        SASI is experimental and gated off by default in Cassandra 4.0 (enable_sasi_indexes: false),
        so the CREATE CUSTOM INDEX ... SASIIndex would be rejected outright and the snapshot bug would
        never be reached. enable_sasi_indexes is a cassandra.yaml startup setting (not CQL- or
        runtime-settable), so we patch the operator-managed K8ssandraCluster CR's cassandraYaml block
        and let the operator perform a rolling restart so the gate opens and stays open (it survives
        the later inject_buggy_image restart too, since it lives in the CR). Patching the
        pod/StatefulSet directly would be reverted by the operator on the next reconcile.
        """
        import json

        cluster = self.app.cluster_name
        ns = self.app.namespace
        patch = json.dumps(
            [
                {
                    "op": "add",
                    "path": "/spec/cassandra/datacenters/0/config/cassandraYaml",
                    "value": {"enable_sasi_indexes": True},
                }
            ]
        )
        logger.info(
            f"[AutoCassandra15134] Enabling SASI on K8ssandraCluster '{cluster}' "
            f"(cassandraYaml.enable_sasi_indexes=true)"
        )
        subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} --type=json -p '{patch}'",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        # Wait for the operator-driven rolling restart to bring the cluster back to Ready before
        # inject_fault runs the SASI CREATE / flush / snapshot.
        logger.info("[AutoCassandra15134] Waiting for cluster to be Ready after SASI rollout")
        self.app._wait_for_cluster_ready(timeout=1800)
        logger.info("[AutoCassandra15134] SASI enabled; cluster Ready")

    def _server_pods(self) -> list[str]:
        """Return the names of the running cass-operator-managed Cassandra server pods.

        The cluster is deployed by the K8ssandra/cass-operator (see _cassandra_cluster_manifest), which
        labels server pods with ``cassandra.datastax.com/cluster=<cluster_name>``. Unlike the
        schema.cql snapshot bugs (13935/19747/20036), the artifact here (the per-sstable SI_*.db file)
        is DATA-derived: with the deployed size:3 / RF=1 topology the 3 rows scatter by token, so the
        flushed sstable (and its SASI index file) may live on any single node. We therefore drive
        flush + snapshot on EVERY server pod and inspect EVERY pod's snapshot dir, rather than picking
        one — otherwise we might flush/snapshot a node that holds no data and observe nothing.
        """
        cluster = self.app.cluster_name
        selector = f"cassandra.datastax.com/cluster={cluster}"
        out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} -l {selector} "
                f"-o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return [p for p in out.split() if p]

    def _exec(self, pod: str, inner: str, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run ``inner`` inside the ``cassandra`` container of ``pod`` via ``bash -lc``."""
        inner = self._authed_cqlsh(inner)
        cmd = f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -lc {subprocess.list2cmdline([inner])}"
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

    @mark_fault_injected
    def inject_fault(self):
        """Drive the CASSANDRA-15134 snapshot reproduction on the buggy 4.0.1 server pod(s).

        Steps (all in ONE live session — no restart between flush and snapshot, which would otherwise
        re-scan the on-disk SASI files into the components list and mask the bug):
          1. Ensure the buggy image is active (no-op when prebuilt_from_stock pre-deployed it).
          2. (post_deploy already enabled SASI in the CR before this point.)
          3. Create keyspace + table + SASI CUSTOM INDEX + INSERT rows (via cqlsh on one pod).
          4. On EVERY server pod: nodetool flush (writes the sstable + builds the SI_*.db) then
             nodetool snapshot (hard-links the sstable components into the snapshot dir).
          5. On EVERY server pod: list the live sstable dir vs the snapshot dir for the table and assert
             the buggy signature — a SI_*.db present in the live dir but ABSENT from the snapshot dir.
        """
        # 1. Make sure the buggy binary is the one running (lifecycle parity with the base class).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra15134] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra15134] Injecting fault: swapping cluster to {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra15134] Buggy image active")

        self.setup_preconditions()

        pods = self._server_pods()
        if not pods:
            logger.warning("[AutoCassandra15134] No Cassandra server pod found — cannot run reproducer")
            return
        logger.info(f"[AutoCassandra15134] Using server pods {pods}")

        # 3. Schema + SASI custom index + rows (run once via cqlsh on the first pod — schema/data
        #    propagate cluster-wide; data lands on whichever node owns each token).
        logger.info("[AutoCassandra15134] Creating keyspace/table + SASI CUSTOM INDEX and inserting rows")
        self._exec(pods[0], f"cqlsh -e {subprocess.list2cmdline([self._SETUP_CQL])}")

        table_glob = f"/var/lib/cassandra/data/{_KEYSPACE}/{_TABLE}-*"
        reproduced_on = []

        for pod in pods:
            # 4. Flush (builds the SI_*.db on disk) then snapshot — same live session, no restart.
            logger.info(f"[AutoCassandra15134] [{pod}] nodetool flush {_KEYSPACE}")
            self._exec(pod, f"nodetool flush {_KEYSPACE}")
            logger.info(f"[AutoCassandra15134] [{pod}] nodetool snapshot -t {_SNAPSHOT_TAG} -kt {_KEYSPACE}.{_TABLE}")
            self._exec(pod, f"nodetool snapshot -t {_SNAPSHOT_TAG} -kt {_KEYSPACE}.{_TABLE}")

            # 5. Compare live sstable dir vs snapshot dir for the SI_*.db component on this pod.
            live = self._exec(pod, f"find {table_glob} -maxdepth 1 -name '*SI_*' 2>/dev/null").stdout.strip()
            snap = self._exec(
                pod,
                f"find {table_glob}/snapshots/{_SNAPSHOT_TAG} -name '*SI_*' 2>/dev/null",
            ).stdout.strip()
            logger.info(
                f"[AutoCassandra15134] [{pod}] live SI files: {live or '(none)'} | "
                f"snapshot SI files: {snap or '(none)'}"
            )

            if live and not snap:
                reproduced_on.append(pod)
                logger.info(
                    f"[AutoCassandra15134] [{pod}] REPRODUCED — SASI index file present in the live "
                    f"sstable dir but ABSENT from snapshot '{_SNAPSHOT_TAG}' (the bug)."
                )

        if reproduced_on:
            logger.info(
                f"[AutoCassandra15134] Reproduced on pod(s) {reproduced_on}: a freshly-flushed "
                f"sstable's SASI index file (SI_*.db) is on disk but missing from the snapshot."
            )
        else:
            logger.warning(
                "[AutoCassandra15134] Did not observe a live-present / snapshot-absent SASI index file "
                "on any server pod — the bug may not be present on this build (or no sstable held data)."
            )
