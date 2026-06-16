"""SAI index build throws NPE on an empty value for a numeric column.

Title:   SAI should avoid attempting to index empty values for numerics and types
         that do not allow them.
JIRA:    https://issues.apache.org/jira/browse/CASSANDRA-20313
Buggy:   5.0.3   ->   Fixed: 5.0.4 (also 6.0-alpha1, 6.0)
Component: Feature/SAI (Storage-Attached Indexing)

Reproduction summary (single node, RF=1):
  1. CREATE TABLE t (k int PRIMARY KEY, v int).
  2. INSERT an EMPTY byte buffer into the int column v via
     INSERT INTO t (k, v) VALUES (0, blobAsInt(0x)).  Int32Type.validate() permits
     remaining()==0, so an empty serialized int is legal at the storage layer (this is
     exactly why the bug exists). The value renders blank but is empty bytes, NOT null.
  3. nodetool flush so the empty value lands in an SSTable (the indexSSTable build path
     is what fails — the empty value must be on disk).
  4. CREATE INDEX t_v_idx ON t (v) USING 'sai'.  The async SAI build over the SSTable
     throws NPE, the build fails, the index is left NOT queryable, and `CREATE INDEX`
     blocks/times out in cqlsh. On fixed 5.0.4 the identical workload builds successfully
     and skips the empty numeric value.

Verbatim buggy signature (from the reproduction evidence log, pod log of cassandra:5.0.3):
  WARN  [SecondaryIndexManagement:1] SecondaryIndexManager.java:843 - Index build of t_v_idx
  failed. Please run full index rebuild to fix it.
  java.util.concurrent.ExecutionException: java.lang.NullPointerException: Cannot invoke
  "org.apache.cassandra.utils.bytecomparable.ByteSource.next()" because "key" is null
      ...
  Caused by: java.lang.NullPointerException: Cannot invoke
  "org.apache.cassandra.utils.bytecomparable.ByteSource.next()" because "key" is null
      at org.apache.cassandra.db.tries.InMemoryTrie.putRecursive(InMemoryTrie.java:904)
      at org.apache.cassandra.index.sai.disk.v1.segment.SegmentTrieBuffer.add(SegmentTrieBuffer.java:69)
      at org.apache.cassandra.index.sai.disk.v1.segment.SegmentBuilder.add(SegmentBuilder.java:195)
      at org.apache.cassandra.index.sai.disk.v1.SSTableIndexWriter.addTerm(SSTableIndexWriter.java:208)
      at org.apache.cassandra.index.sai.disk.StorageAttachedIndexWriter.addRow(StorageAttachedIndexWriter.java:257)
      at org.apache.cassandra.index.sai.StorageAttachedIndexBuilder.indexSSTable(StorageAttachedIndexBuilder.java:188)
      at org.apache.cassandra.index.sai.StorageAttachedIndexBuilder.build(StorageAttachedIndexBuilder.java:118)
      ...

Reproduction shape: nodetool / flush sequence (NOT a pure-CQL continuous reproducer).
  The bug only fires when the empty value is read back from an SSTable during the SAI index
  build, so the reproduction requires `nodetool flush` between the INSERT and `CREATE INDEX`,
  on the Cassandra server pod. The framework's CQL-only reproducer/mitigation path (a separate
  cassandra:4.1 client pod that only pipes CQL into cqlsh) CANNOT run `nodetool flush`, and
  re-running `CREATE INDEX` in a loop would error on "index already exists" regardless of
  whether the bug is fixed (a false NotReady). So this is encoded as the decision-tree
  "nodetool / flush sequence" shape: inject_fault() is overridden to drive the full
  CREATE/INSERT + flush + CREATE INDEX sequence via kubectl-exec on the server pod, and
  continuous_reproducer is left False (diagnosis-only, mitigation_oracle = None), like the
  other server-side Cassandra bug problems (e.g. auto_cassandra_20036).
"""

import base64
import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KEYSPACE = "repro_20313"


class AutoCassandra20313(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.3"
    source_git_ref = "cassandra-5.0.3"
    # 5.0.3 already ships the bug (fix landed in 5.0.4), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/index/sai/StorageAttachedIndexBuilder.java"
    root_cause_description = (
        "Building a Storage-Attached Index (SAI) over an SSTable that contains an EMPTY value for a "
        "numeric column (e.g. an int written from EMPTY_BYTE_BUFFER, which Int32Type.validate() "
        "permits) throws a NullPointerException and the async index build fails, leaving the index "
        "not queryable. During StorageAttachedIndexBuilder.indexSSTable, the empty value flows into "
        "the trie-segment writer (SSTableIndexWriter.addTerm -> SegmentBuilder.add -> "
        "SegmentTrieBuffer.add -> InMemoryTrie.putRecursive) where the ByteComparable/ByteSource key "
        "derived from the empty term is null, so InMemoryTrie.putRecursive NPEs ('Cannot invoke "
        "ByteSource.next() because key is null'). SecondaryIndexManager then logs 'Index build of "
        "t_v_idx failed' and CREATE INDEX never becomes queryable. The fix is for SAI to skip / "
        "avoid attempting to index empty values for numerics and other types that do not allow them."
    )

    # Canonical buggy steps (documentation / human-readable record). The actual orchestration —
    # CREATE/INSERT, `nodetool flush` (so the empty value lands in an SSTable), then `CREATE INDEX` —
    # is driven by inject_fault() below, because the required `nodetool flush` step is NOT expressible
    # as a CQL string piped into cqlsh.
    reproducer = """
-- Setup (run via cqlsh on the server pod):
CREATE KEYSPACE IF NOT EXISTS repro_20313 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro_20313.t (k int PRIMARY KEY, v int);
INSERT INTO repro_20313.t (k, v) VALUES (0, blobAsInt(0x));   -- empty bytes into the int column (NOT null)

-- Persist the empty value to an SSTable (run via nodetool on the server pod):
--   nodetool flush repro_20313 t

-- Trigger the bug (run via cqlsh on the server pod):
CREATE INDEX t_v_idx ON repro_20313.t (v) USING 'sai';
-- Buggy 5.0.3: the async SAI build over the SSTable NPEs (InMemoryTrie.putRecursive, key null);
--   the index build fails, the index is left not queryable, and CREATE INDEX times out in cqlsh:
--     SecondaryIndexManager.java:843 - Index build of t_v_idx failed. Please run full index rebuild to fix it.
--     java.lang.NullPointerException: Cannot invoke "...ByteSource.next()" because "key" is null
-- Fixed 5.0.4: CREATE INDEX succeeds immediately; the empty numeric value is skipped.
"""
    # Server-side (nodetool/flush) bug: there is NO pure-CQL probe the CQL-only reproducer pod can run
    # to detect it (it cannot flush, and re-CREATE INDEX would error on "already exists"), so this is
    # diagnosis-only. Setting continuous_reproducer True here would deploy a mitigation pod that runs
    # these steps as CQL and stay permanently NotReady, which is worse than no mitigation oracle.
    continuous_reproducer = False
    # No expected_output: this is a failed-index-build / error bug, not a wrong-result bug.

    # ── CQL/nodetool driven on the server pod ─────────────────────────────────

    _SETUP_CQL = (
        f"CREATE KEYSPACE IF NOT EXISTS {_KEYSPACE} "
        "WITH replication = {'class':'SimpleStrategy','replication_factor':1}; "
        f"CREATE TABLE IF NOT EXISTS {_KEYSPACE}.t (k int PRIMARY KEY, v int); "
        f"INSERT INTO {_KEYSPACE}.t (k, v) VALUES (0, blobAsInt(0x));"
    )

    _CREATE_INDEX_CQL = f"CREATE INDEX IF NOT EXISTS t_v_idx ON {_KEYSPACE}.t (v) USING 'sai';"

    def _server_pod(self) -> str | None:
        """Return the name of a Running cass-operator-managed Cassandra server pod.

        The cluster is deployed by the K8ssandra/cass-operator (see _cassandra_cluster_manifest),
        which labels server pods with ``app.kubernetes.io/name=cassandra``. The reproduction is
        purely local to a single node, so any one running server pod is sufficient.
        """
        out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} "
                f"-l app.kubernetes.io/name=cassandra "
                f"--field-selector=status.phase=Running "
                f"-o jsonpath='{{.items[0].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return out or None

    def _superuser_creds(self) -> tuple[str, str]:
        """Read the K8ssandra-managed superuser credentials from the cluster secret.

        K8ssandra enables PasswordAuthenticator by default and generates a
        ``<cluster_name>-superuser`` secret; fall back to cassandra/cassandra.
        """
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

    def _exec_cql(self, pod: str, u_b64: str, p_b64: str, cql: str, timeout: int = 180) -> subprocess.CompletedProcess:
        """Pipe CQL into cqlsh inside the ``cassandra`` container of ``pod``, with superuser creds."""
        return subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f'cqlsh -u "$U" -p "$P" --request-timeout=60'
            f"'",
            shell=True,
            capture_output=True,
            text=True,
            input=cql,
            timeout=timeout,
        )

    def _exec_flush(self, pod: str, u_b64: str, p_b64: str, timeout: int = 180) -> None:
        """Run ``nodetool flush <ks> t`` on the server pod so the empty value lands in an SSTable.

        cass-management-api nodetool needs the JMX superuser creds; pass them best-effort and
        fall back to a bare invocation.
        """
        subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f'nodetool -u "$U" -pw "$P" flush {_KEYSPACE} t || nodetool flush {_KEYSPACE} t'
            f"'",
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @mark_fault_injected
    def inject_fault(self):
        """Drive the CASSANDRA-20313 SAI-index-build reproduction on the server pod.

        Steps (all on the buggy 5.0.3 server pod, so the buggy build itself does the index build):
          1. Ensure the buggy image is active (no-op when prebuilt_from_stock pre-deployed it).
          2. CREATE KEYSPACE/TABLE + INSERT an empty value into the int column (blobAsInt(0x)).
          3. nodetool flush -> the empty value lands in an SSTable.
          4. CREATE INDEX ... USING 'sai' -> the async SAI build over that SSTable NPEs; the build
             fails and the index is left not queryable (CREATE INDEX blocks/times out in cqlsh).
        """
        # 1. Make sure the buggy binary is the one running (lifecycle parity with the base class).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra20313] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra20313] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra20313] Buggy image active")

        self.setup_preconditions()

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra20313] No running Cassandra server pod found — cannot run reproducer")
            return
        logger.info(f"[AutoCassandra20313] Using server pod {pod}")

        try:
            username, password = self._superuser_creds()
            u_b64 = base64.b64encode(username.encode()).decode()
            p_b64 = base64.b64encode(password.encode()).decode()

            # 2. Schema + the empty value into the int column.
            logger.info("[AutoCassandra20313] Creating keyspace/table + inserting empty int value (blobAsInt(0x))")
            self._exec_cql(pod, u_b64, p_b64, self._SETUP_CQL)
            # Let the schema/write settle before flushing.
            time.sleep(3)

            # 3. Flush so the empty value is in an SSTable (the indexSSTable build path is what fails).
            logger.info("[AutoCassandra20313] nodetool flush -> empty value lands in an SSTable")
            self._exec_flush(pod, u_b64, p_b64)
            time.sleep(2)

            # 4. CREATE INDEX triggers the async SAI build over the SSTable -> NPE on the buggy build.
            #    cqlsh blocks until the index becomes queryable and times out (the build never finishes),
            #    so use a short request timeout and tolerate the expected failure/timeout.
            logger.info("[AutoCassandra20313] CREATE INDEX ... USING 'sai' (expect SAI build NPE on 5.0.3)")
            result = self._exec_cql(pod, u_b64, p_b64, self._CREATE_INDEX_CQL, timeout=120)
            combined = (result.stdout + result.stderr).strip()
            if result.returncode != 0 or "OperationTimedOut" in combined or "timeout" in combined.lower():
                logger.info(f"[AutoCassandra20313] Reproduced (index build did not become queryable): {combined[:300]}")
            else:
                logger.warning(
                    f"[AutoCassandra20313] CREATE INDEX returned success (rc={result.returncode}); "
                    f"the SAI build may not have hit the empty-value path: {combined[:300]}"
                )
        except subprocess.TimeoutExpired:
            # cqlsh blocking on a never-queryable index is the buggy behaviour itself.
            logger.info(
                "[AutoCassandra20313] CREATE INDEX timed out waiting for the index to become queryable (expected on 5.0.3)"
            )
        except Exception as e:  # tolerate exec hiccups; the bug surfaces in the server log regardless
            logger.warning(f"[AutoCassandra20313] inject_fault raised (continuing): {e}")
