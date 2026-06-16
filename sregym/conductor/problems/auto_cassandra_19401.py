"""CASSANDRA-19401: nodetool import silently imports nothing from a flat source directory.

Title: Nodetool import expects directory structure
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-19401
Buggy: 4.1.4  ->  Fixed: 4.0.13, 4.1.5, 5.0-rc1, 6.0
Components: Local/SSTable

Reproduction summary (single node, NODETOOL/FILESYSTEM SEQUENCE — not pure CQL):
  The 4.1 docs claim `nodetool import` does NOT require SSTables to live in a
  `<keyspace>/<table>` directory because the keyspace/table are given on the command
  line. In reality, on 4.1.4, when the source directory is a FLAT directory whose parent
  dir names do NOT match `<keyspace>/<table>`, `nodetool import --copy-data` silently
  imports nothing (nodetool exits 0 with no stdout) and the table stays empty. Moving the
  exact same SSTables into a `.../<keyspace>/<table>/`-named directory makes the import
  succeed, and on 4.1.5 the identical flat-path import succeeds — isolating the failure to
  import-path handling in SSTableImporter.

Verbatim buggy signature (server-side INFO log on cassandra:4.1.4):
  SSTableImporter.java:173 - No new SSTables were found for repro19401ks/t

This is encoded with a custom inject_fault() (kubectl-exec into the Cassandra server pod)
because the reproduction needs nodetool flush/import plus on-disk SSTable staging that a
pure-CQL `reproducer` string cannot express. The staging dir must be chown'd to the
Cassandra daemon uid (999) — kubectl exec runs as root, so without the chown the importer
fails earlier with `Insufficient permissions on directory` (a separate guard at
SSTableImporter.java:242, NOT this bug). It is diagnosis-only (continuous_reproducer=False):
the standard continuous-reproducer pod is a separate CQL client that cannot run nodetool or
see the server's data dir, so a CQL-loop mitigation probe could not observe this bug.

Runtime note (operator ring): on the K8ssandra operator runtime the buggy image is applied as a
3-node rolling restart, and inject_buggy_image() returns on the FIRST Ready pod (leaving the ring
mid-restart with both operators scaled to 0). inject_fault() therefore waits for the ring to return
to all-UN + a single schema version before running the reproduction, and the verification SELECT
retries past the known spurious ring-restart errors ("Cannot achieve consistency ONE", NoHostAvailable)
— those are restart races, NOT this bug. The keyspace uses replication_factor=3 so the single server
pod that flushes/stages/imports holds every row's SSTables locally (an RF=1 table would scatter the 5
rows across the ring and the local flat-dir staging step could find no SSTables to stage). The bug is
purely SSTableImporter directory-name handling and is independent of replication factor.
"""

import base64
import json
import logging
import re
import shlex
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KEYSPACE = "repro19401ks"
_TABLE = "t"
_STAGING_DIR = "/tmp/staging"
_DATA_DIR = "/var/lib/cassandra/data"
_CASS_UID = "999"  # the cassandra daemon runs as uid 999 inside the pod

# CQL run before the flush/import sequence: create the keyspace + table and insert 5 rows.
# RF=3 so the single server pod we flush/stage/import on holds every row's SSTables locally on
# the 3-node operator ring (an RF=1 table would scatter the rows and the flat-dir staging could
# find none). The bug is SSTableImporter directory handling, independent of replication factor.
_SETUP_CQL = (
    "DROP KEYSPACE IF EXISTS repro19401ks; "
    "CREATE KEYSPACE repro19401ks WITH REPLICATION = "
    "{'class': 'SimpleStrategy', 'replication_factor': 3}; "
    "CREATE TABLE repro19401ks.t (id int PRIMARY KEY, v text); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (1, 'a'); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (2, 'b'); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (3, 'c'); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (4, 'd'); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (5, 'e');"
)

# CQL run after staging: empty the table so the (failed) import result is client-visible.
_TRUNCATE_CQL = "TRUNCATE repro19401ks.t;"

# CQL to observe the buggy result (table stays empty after the flat-path import).
_COUNT_CQL = "SELECT count(*) FROM repro19401ks.t;"

# Verbatim documented signature (server-side INFO log). The :NNN frame can shift across builds; the
# message text is the stable, version-independent part.
_SIGNATURE = f"No new SSTables were found for {_KEYSPACE}/{_TABLE}"

# Transient errors produced while the ring is still rolling after the buggy-image swap. These are
# NOT the bug — the verification SELECT retries past them once the ring is back to all-UN.
_SPURIOUS = re.compile(
    r"Cannot achieve consistency|NoHostAvailable|Unavailable|OperationTimedOut|"
    r"Connection.*(defunct|refused)|coordinator are down|timed out|Bad credentials|"
    r"rolling restart|cannot be reached|Connection error",
    re.IGNORECASE,
)


class AutoCassandra19401(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.4"
    source_git_ref = "cassandra-4.1.4"
    # 4.1.4 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/SSTableImporter.java"
    root_cause_description = (
        "nodetool import (StorageService.importNewSSTables -> SSTableImporter) does not honor the "
        "documented contract that SSTables may live in a flat source directory when keyspace/table "
        "are passed on the command line. On 4.1.4, SSTableImporter only discovers SSTables whose "
        "parent directory is named <keyspace>/<table>; given a flat source dir it finds nothing and "
        "logs 'No new SSTables were found for repro19401ks/t' (SSTableImporter.java:173), so "
        "`nodetool import --copy-data` exits 0 having imported nothing and the table stays empty. "
        "The same SSTables import correctly from a <keyspace>/<table>-named directory on 4.1.4, and "
        "the identical flat-path import succeeds on 4.1.5, pinning the defect to SSTableImporter's "
        "directory-name-dependent SSTable discovery. The fix makes import discover SSTables in the "
        "given source directory regardless of its directory naming."
    )

    # Documented buggy reproducer (run programmatically by inject_fault inside the server pod,
    # because it mixes CQL with nodetool flush/import and on-disk SSTable staging). The discriminator
    # is the FLAT source dir /tmp/staging whose parent dirs are NOT <keyspace>/<table>.
    reproducer = """
-- 1. Schema + data (5 rows), then flush to write SSTables to disk (RF=3 so one node holds all):
DROP KEYSPACE IF EXISTS repro19401ks;
CREATE KEYSPACE repro19401ks WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 3};
CREATE TABLE repro19401ks.t (id int PRIMARY KEY, v text);
INSERT INTO repro19401ks.t (id, v) VALUES (1, 'a');
INSERT INTO repro19401ks.t (id, v) VALUES (2, 'b');
INSERT INTO repro19401ks.t (id, v) VALUES (3, 'c');
INSERT INTO repro19401ks.t (id, v) VALUES (4, 'd');
INSERT INTO repro19401ks.t (id, v) VALUES (5, 'e');
-- nodetool flush repro19401ks t
--
-- 2. Stage the FULL SSTable component set into a FLAT dir (no <keyspace>/<table> subdirs),
--    then chown to the cassandra daemon uid so the importer can write to it:
--   mkdir -p /tmp/staging
--   cp $(find /var/lib/cassandra/data/repro19401ks/t-*/ -maxdepth 1 -type f) /tmp/staging/
--   chown -R 999:999 /tmp/staging && chmod -R u+rwX /tmp/staging
--
-- 3. TRUNCATE so the import result is client-visible:
TRUNCATE repro19401ks.t;
--
-- 4. Import from the FLAT dir -> silently imports nothing (nodetool exits 0, no stdout),
--    server logs "SSTableImporter.java:173 - No new SSTables were found for repro19401ks/t":
--   nodetool import --copy-data repro19401ks t /tmp/staging
--
-- 5. Table is still empty (count 0) -> this is the bug:
SELECT count(*) FROM repro19401ks.t;
"""
    # Diagnosis-only: the standard continuous-reproducer pod is a separate CQL client that cannot
    # run nodetool or read the server's SSTable data dir, so it cannot reproduce/observe this bug.
    continuous_reproducer = False

    # ── Fault injection (custom: nodetool/filesystem sequence inside the server pod) ──────────

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, then run the flat-path nodetool import sequence in-pod.

        Mirrors the GenericCustomBuildProblem image-swap guard, then performs the full
        buggy reproduction (CQL setup -> flush -> flat-dir staging + chown 999 -> TRUNCATE ->
        flat-path import) directly inside the Cassandra server pod via kubectl exec, since the
        bug requires nodetool and on-disk SSTable handling that a CQL reproducer cannot express.

        The swap is a 3-node rolling restart and inject_buggy_image() returns on the FIRST Ready
        pod, so the reproduction waits for the ring to fully re-stabilize (all-UN + a single schema
        version) before it touches the cluster, and the final verification SELECT retries past the
        spurious post-restart ring errors. Only the documented signatures count: the server-side
        INFO log 'No new SSTables were found for repro19401ks/t' and the empty (count 0) table.
        """
        if self._predeployed_buggy:
            logger.info("[AutoCassandra19401] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra19401] Injecting fault: swapping cluster to {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra19401] Buggy image active")

        self.setup_preconditions()

        # Wait out the post-swap rolling restart BEFORE running any CQL/nodetool — running now
        # races the restart and fails spuriously ("Cannot achieve consistency ONE during rolling
        # restart"), which is NOT the bug.
        self._wait_for_ring_stable()

        pod = self._ready_ring_pod() or self._get_cassandra_pod()
        if not pod:
            logger.warning("[AutoCassandra19401] No Cassandra pod found — cannot run reproducer")
            return

        # 1. Schema + 5 rows (RF=3), then flush so SSTables land under the table's data dir on
        #    this pod (RF=3 guarantees the chosen pod holds all rows locally on the 3-node ring).
        logger.info("[AutoCassandra19401] Creating keyspace/table and inserting 5 rows (on pod %s)", pod)
        self._run_cql_in_pod(pod, _SETUP_CQL)
        logger.info("[AutoCassandra19401] Flushing %s.%s to SSTables on disk", _KEYSPACE, _TABLE)
        self._exec_in_pod(pod, ["nodetool", "flush", _KEYSPACE, _TABLE])

        # 2. Stage the full SSTable component set into a FLAT dir (no <keyspace>/<table>
        #    subdirs) and chown it to the daemon uid (999); kubectl exec runs as root, and
        #    without this the importer fails earlier with "Insufficient permissions on
        #    directory" (SSTableImporter.java:242) — a different guard, not this bug.
        logger.info("[AutoCassandra19401] Staging SSTables into flat dir %s (chown %s)", _STAGING_DIR, _CASS_UID)
        stage_cmd = (
            f"set -e; rm -rf {_STAGING_DIR}; mkdir -p {_STAGING_DIR}; "
            f"cp $(find {_DATA_DIR}/{_KEYSPACE}/{_TABLE}-*/ -maxdepth 1 -type f) {_STAGING_DIR}/; "
            f"chown -R {_CASS_UID}:{_CASS_UID} {_STAGING_DIR}; chmod -R u+rwX {_STAGING_DIR}; "
            f"ls -la {_STAGING_DIR}"
        )
        self._exec_in_pod(pod, ["bash", "-c", stage_cmd])

        # 3. TRUNCATE so the (failed) import is client-visible as an empty table.
        logger.info("[AutoCassandra19401] Truncating %s.%s", _KEYSPACE, _TABLE)
        self._run_cql_in_pod(pod, _TRUNCATE_CQL)

        # 4. Import from the FLAT dir — on 4.1.4 this silently imports nothing.
        logger.info("[AutoCassandra19401] Running flat-path nodetool import (expected to silently no-op)")
        self._exec_in_pod(pod, ["nodetool", "import", "--copy-data", _KEYSPACE, _TABLE, _STAGING_DIR])

        # 5. Capture the documented buggy signatures on the (re-)stabilized ring.
        self._verify_buggy_signature(pod)

    def _verify_buggy_signature(self, pod: str):
        """Capture both documented signatures: the verbatim server-side INFO log and the
        client-visible empty table (count 0). The SELECT retries past spurious ring-restart
        errors after re-confirming the ring is stable."""
        # (a) Verbatim server-side INFO log — the primary documented signature.
        sig_line = self._grep_signature(pod)
        if sig_line:
            logger.warning("[AutoCassandra19401] DOCUMENTED SIGNATURE (server log): %s", sig_line)
        else:
            logger.warning(
                "[AutoCassandra19401] Documented server-log signature %r NOT found in system.log", _SIGNATURE
            )

        # (b) Client-visible wrong outcome: the table is still empty after the flat-path import.
        self._wait_for_ring_stable(timeout=600)
        count_out = self._count_with_retries(pod)
        logger.warning(
            "[AutoCassandra19401] DOCUMENTED SIGNATURE (client): table still empty after flat-path "
            "import — count(*) result: %s",
            count_out.replace("\n", " | ") if count_out else "<no output>",
        )

    # ── Ring stabilization & signature capture ─────────────────────────────────────────────

    def _ring_server_pods(self) -> list[str]:
        """All Cassandra server pod names for this cluster (K8ssandra instance label)."""
        result = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}'",
            shell=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.replace("'", "").split()

    def _ready_ring_pod(self) -> str | None:
        """A Running, fully-ready server pod with no deletionTimestamp.

        A pod can report Ready while mid-Terminating during the operator's rolling restart, and
        exec'ing into it yields NoHostAvailable — so skip any pod with a deletionTimestamp.
        """
        result = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} -o json",
            shell=True,
            capture_output=True,
            text=True,
        )
        try:
            data = json.loads(result.stdout)
        except Exception:
            return None
        for item in data.get("items", []):
            meta = item.get("metadata", {})
            if meta.get("deletionTimestamp"):
                continue
            status = item.get("status", {})
            if status.get("phase") != "Running":
                continue
            css = status.get("containerStatuses", [])
            if css and all(c.get("ready") for c in css):
                return meta.get("name")
        return None

    def _wait_for_ring_stable(self, timeout: int = 1200) -> bool:
        """Wait until the ring is back to all-UN with a single schema version after the swap.

        inject_buggy_image() returns on the first Ready pod and the operator-override path leaves
        both operators scaled to 0 (the pod-admission webhook is then down, so a pod deleted by the
        rolling restart is never recreated). Scale the operators back up, then poll nodetool until
        every node is UN (Up/Normal) and the cluster reports exactly one schema version.
        """
        logger.info("[AutoCassandra19401] Scaling operators up; waiting for ring to re-stabilize (<=%ds)", timeout)
        try:
            self.app._scale_operator_up()
        except Exception as e:
            logger.warning("[AutoCassandra19401] operator scale-up raised: %s", e)

        deadline = time.time() + timeout
        while time.time() < deadline:
            pod = self._ready_ring_pod()
            if pod:
                _, status_out, _ = self._exec_in_pod(pod, ["nodetool", "status"], quiet=True)
                node_lines = [ln.strip() for ln in status_out.splitlines() if re.match(r"^[UD][NLJM]\s", ln.strip())]
                un = sum(1 for ln in node_lines if ln.startswith("UN"))
                _, desc_out, _ = self._exec_in_pod(pod, ["nodetool", "describecluster"], quiet=True)
                schemas = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", desc_out))
                logger.info(
                    "[AutoCassandra19401] ring check: UN=%d total=%d schema_versions=%d",
                    un,
                    len(node_lines),
                    len(schemas),
                )
                if un >= 3 and un == len(node_lines) and len(schemas) == 1:
                    logger.info("[AutoCassandra19401] Ring stable (3x UN, single schema version)")
                    return True
            time.sleep(20)
        logger.warning("[AutoCassandra19401] Ring did not fully stabilize within %ds — proceeding best-effort", timeout)
        return False

    def _grep_signature(self, pod: str) -> str:
        """Return the verbatim server-side INFO log line(s) for the documented signature, if any."""
        _, out, _ = self._exec_in_pod(
            pod,
            ["bash", "-c", f"grep -aF {shlex.quote(_SIGNATURE)} /var/log/cassandra/system.log | tail -5"],
            quiet=True,
        )
        return out.strip()

    def _count_with_retries(self, pod: str, retries: int = 10, sleep: int = 15) -> str:
        """Run the verification count SELECT, retrying past spurious ring-restart errors."""
        last = ""
        for attempt in range(retries):
            _, out, err = self._run_cql_in_pod(pod, _COUNT_CQL, quiet=True)
            combined = f"{out}\n{err}"
            if "rows)" in out and not _SPURIOUS.search(combined):
                logger.info("[AutoCassandra19401] count SELECT result: %s", out.strip()[:200])
                return out.strip()
            if not _SPURIOUS.search(combined):
                return out.strip()
            logger.info(
                "[AutoCassandra19401] count SELECT hit spurious ring error (attempt %d/%d), retrying",
                attempt + 1,
                retries,
            )
            np = self._ready_ring_pod()
            if np:
                pod = np
            last = combined.strip()
            time.sleep(sleep)
        return last

    # ── Helpers ───────────────────────────────────────────────────────────────────────────

    def _get_cassandra_pod(self) -> str:
        result = subprocess.run(
            f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip().strip("'")

    def _get_cql_credentials(self) -> tuple[str, str]:
        """Read the K8ssandra superuser credentials from the cluster's secret."""
        secret_name = f"{self.app.cluster_name}-superuser"
        username = subprocess.run(
            f"kubectl get secret {secret_name} -n {self.namespace} -o jsonpath='{{.data.username}}'",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        password = subprocess.run(
            f"kubectl get secret {secret_name} -n {self.namespace} -o jsonpath='{{.data.password}}'",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return base64.b64decode(username).decode(), base64.b64decode(password).decode()

    def _run_cql_in_pod(self, pod: str, cql: str, quiet: bool = False) -> tuple[int, str, str]:
        """Run CQL via cqlsh inside the server pod (K8ssandra requires auth). Returns
        (returncode, stdout, stderr)."""
        username, password = self._get_cql_credentials()
        u_b64 = base64.b64encode(username.encode()).decode()
        p_b64 = base64.b64encode(password.encode()).decode()
        result = subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f'cqlsh -u "$U" -p "$P" --request-timeout=30'
            f"'",
            shell=True,
            capture_output=True,
            text=True,
            input=cql,
        )
        if not quiet:
            if result.stdout.strip():
                logger.info("[AutoCassandra19401] cqlsh stdout: %s", result.stdout.strip()[:400])
            if result.returncode != 0:
                logger.warning(
                    "[AutoCassandra19401] cqlsh exited %s: %s", result.returncode, result.stderr.strip()[:400]
                )
        return result.returncode, result.stdout, result.stderr

    def _exec_in_pod(self, pod: str, argv: list[str], quiet: bool = False) -> tuple[int, str, str]:
        """Run a command inside the server pod's cassandra container. Returns
        (returncode, stdout, stderr)."""
        result = subprocess.run(
            ["kubectl", "exec", "-n", self.namespace, pod, "-c", "cassandra", "--", *argv],
            capture_output=True,
            text=True,
        )
        if not quiet:
            joined = " ".join(argv)
            if result.stdout.strip():
                logger.info("[AutoCassandra19401] `%s` stdout: %s", joined, result.stdout.strip()[:400])
            if result.returncode != 0:
                logger.warning(
                    "[AutoCassandra19401] `%s` exited %s: %s", joined, result.returncode, result.stderr.strip()[:400]
                )
        return result.returncode, result.stdout, result.stderr
