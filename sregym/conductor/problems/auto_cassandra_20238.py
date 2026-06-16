"""SAI returns a missing row after partition delete + re-insert + flush.

Title: Correct the default behavior of compareTo() when comparing WIDE and STATIC PrimaryKeys.
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-20238
Component: Feature/SAI — Correctness / Unrecoverable Corruption / Loss (Critical).

Buggy: cassandra:5.0.3   Fixed: cassandra:5.0.4 (also 6.0-alpha1, 6.0).

Reproduction (single node, RF=1):
  Table with composite partition key ((pk0,pk1), ck0), a static column s1, value v0, and an
  SAI index on the PARTITION-KEY column pk0. Apply (pinned timestamps to fix the ordering):
  UPDATE (s1,v0 at ck0=0, ts=1000) -> partition DELETE (pk0,pk1, ts=2000) -> UPDATE creating
  the surviving row at ck0=1 (v0=1, ts=3000) -> nodetool flush (the defect is on the on-disk
  SAI path). Then `SELECT * WHERE v0=1 AND pk0=0 ALLOW FILTERING` must return that 1 row, but
  the buggy SAI path returns 0. A plain (non-SAI) read `WHERE pk0=0 AND pk1=1` proves the row
  physically exists on disk, so this is a wrong-result/missing-row bug, not data loss.

Verbatim buggy signature (from the reproduction evidence log):
  Buggy SAI result `(0 rows)` from
    `SELECT * FROM repro20238_ks.tbl WHERE v0=1 AND pk0=0 ALLOW FILTERING;`
  on 5.0.3, while the plain read
    `SELECT * FROM repro20238_ks.tbl WHERE pk0=0 AND pk1=1;`
  returns the row `0 | 1 | 1 | null | 1`.

Shape: wrong-result bug that requires a `nodetool flush` between the writes and the SELECT
(the SAI defect manifests only on the on-disk index path). The CQL-only continuous reproducer
pod cannot itself flush, so inject_fault() is overridden to do the writes, run the flush via
`kubectl exec`, fire the SAI SELECT, and then deploy a SELECT-only continuous reproducer that
keeps querying the already-flushed on-disk state. expected_output is the BUGGY value `(0 rows)`
(probe greps for it: Ready = bug present, NotReady = fixed).

Runtime note (operator ring): the buggy image is applied as a 3-node rolling restart and
inject_buggy_image() returns on the FIRST Ready pod, so inject_fault() waits for the ring to
return to all-UN + a single schema version before staging anything (the earlier inject-phase SAI
SELECT raced the restart and failed with spurious NoHostAvailable — that is a restart race, NOT
the bug). The writes use RF=1, so the partition lives on exactly one node; `nodetool flush` is
therefore issued on ALL ring pods so whichever node owns the partition is forced onto the on-disk
SAI path. The verification SAI SELECT (and the corroborating plain read) then run via cqlsh inside
the server pod against that flushed on-disk state — never a fresh un-flushed memtable — and retry
past spurious ring-restart errors before capturing the documented `(0 rows)`.
"""

import base64
import json
import logging
import re
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra20238(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.3"
    source_git_ref = "cassandra-5.0.3"
    # 5.0.3 already ships the bug (fix landed in 5.0.4), so deploy the stock image
    # instead of a ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/index/sai/utils/PrimaryKey.java"
    root_cause_description = (
        "An SAI ALLOW FILTERING query returns a missing row after a partition is deleted and a "
        "row is re-inserted at a surviving clustering key, then flushed to disk. With a static "
        "column present and an SAI index on a partition-key column, `SELECT * WHERE v0=1 AND "
        "pk0=0 ALLOW FILTERING` returns (0 rows) on 5.0.3 even though a plain (non-SAI) read of "
        "the same partition returns the row, proving it is physically on disk. The root cause is "
        "in src/java/org/apache/cassandra/index/sai/utils/PrimaryKey.java: the default compareTo() "
        "behavior when comparing WIDE and STATIC PrimaryKeys is wrong, so on the on-disk SAI path "
        "the surviving wide row is incorrectly ordered/skipped relative to the static-row boundary "
        "left by the partition delete, and the matching row is filtered out of the result. Fixed "
        "in 5.0.4 (and 6.0-alpha1/6.0)."
    )

    # ── Reproducer pieces ──────────────────────────────────────────────────────
    # Pinned timestamps guarantee UPDATE(1000) < partition-DELETE(2000) < final UPDATE(3000),
    # so a timestamp collision can't delete the ck0=1 row "for the right reason". The static
    # column s1 is essential — removing it makes the bug vanish (per the reporter).
    _SETUP_CQL = """
CREATE KEYSPACE IF NOT EXISTS repro20238_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro20238_ks.tbl (pk0 int, pk1 int, ck0 int, s1 int static, v0 int, PRIMARY KEY ((pk0, pk1), ck0));
CREATE INDEX IF NOT EXISTS tbl_pk0 ON repro20238_ks.tbl(pk0) USING 'sai';
UPDATE repro20238_ks.tbl USING TIMESTAMP 1000 SET s1=0, v0=0 WHERE pk0=0 AND pk1=1 AND ck0=0;
DELETE FROM repro20238_ks.tbl USING TIMESTAMP 2000 WHERE pk0=0 AND pk1=1;
UPDATE repro20238_ks.tbl USING TIMESTAMP 3000 SET v0=1 WHERE pk0=0 AND pk1=1 AND ck0=1;
"""

    # Fully-qualified so a SELECT-only run.cql needs no USE statement. On 5.0.3 (post-flush)
    # this returns (0 rows); on 5.0.4 it returns the 1 row.
    _SAI_SELECT = "SELECT * FROM repro20238_ks.tbl WHERE v0=1 AND pk0=0 ALLOW FILTERING;"

    # Plain (non-SAI) single-partition read. After flush this returns the surviving on-disk row
    # `0 | 1 | 1 | null | 1`, proving the row physically exists — so the SAI `(0 rows)` is a
    # wrong-result/missing-row bug, not data loss.
    _PLAIN_SELECT = "SELECT * FROM repro20238_ks.tbl WHERE pk0=0 AND pk1=1;"

    # The documented end-to-end buggy path (writes -> flush -> SAI SELECT). The flush is not a
    # CQL statement, so it is annotated as a comment; inject_fault() performs it via kubectl exec.
    reproducer = (
        _SETUP_CQL
        + "-- nodetool flush repro20238_ks tbl   (run via kubectl exec in inject_fault; the SAI defect is on the on-disk path)\n"
        + _SAI_SELECT
        + "\n"
    )

    continuous_reproducer = True
    # Wrong-result bug: the buggy SAI query returns NO matching row, i.e. its output contains
    # the literal "(0 rows)". The mitigation probe greps for this buggy value, so the reproducer
    # pod is Ready while the bug is present and NotReady once it is fixed (the SAI query then
    # returns the row and "(0 rows)" no longer appears).
    expected_output = "(0 rows)"

    # Transient errors produced while the ring is still rolling after the buggy-image swap — these
    # are NOT the bug; the verification SELECT retries past them once the ring is back to all-UN.
    _SPURIOUS = re.compile(
        r"Cannot achieve consistency|NoHostAvailable|Unavailable|OperationTimedOut|"
        r"Connection.*(defunct|refused)|coordinator are down|timed out|Bad credentials|"
        r"rolling restart|cannot be reached|Connection error",
        re.IGNORECASE,
    )

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, stage writes, flush to disk, fire the SAI SELECT, then
        deploy a SELECT-only continuous reproducer.

        Fully overrides the base inject_fault(): the CQL-only continuous reproducer pod cannot
        issue `nodetool flush`, and the defect only manifests on the on-disk SAI path, so the
        flush must happen here exactly once. The continuous pod then keeps querying the
        already-flushed on-disk state with a SELECT-only workload (re-applying the writes in the
        loop would land the row in a fresh memtable, whose buggy read path may NOT miss the row,
        masking the bug).

        The swap is a 3-node rolling restart and inject_buggy_image() returns on the FIRST Ready
        pod, so the writes/flush/SELECT wait for the ring to re-stabilize (all-UN + a single schema
        version) first — otherwise the SAI SELECT races the restart and fails spuriously with
        NoHostAvailable (NOT the bug). The flush is issued on every ring pod so whichever node owns
        the RF=1 partition is forced onto the on-disk SAI path, and the SAI SELECT runs via cqlsh
        inside the server pod against that flushed state (never a fresh memtable), retrying past
        spurious ring-restart errors before capturing the documented `(0 rows)`.
        """
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra20238] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra20238] Injecting fault: swapping to buggy image {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra20238] Buggy image active")

        # Wait out the post-swap rolling restart before staging/reading — running now races the
        # restart and the SAI SELECT fails spuriously (NoHostAvailable), which is NOT the bug.
        self._wait_for_ring_stable()

        pod = self._ready_ring_pod()
        if not pod:
            logger.warning("[AutoCassandra20238] No ready Cassandra pod found — cannot run reproducer")
            self.app.deploy_continuous_reproducer(self._SAI_SELECT, self.expected_output)
            return

        logger.info("[AutoCassandra20238] Staging schema + pinned-timestamp writes (on pod %s)", pod)
        self._run_cql_in_pod(pod, self._SETUP_CQL)

        # RF=1 → the (pk0=0,pk1=1) partition lives on exactly one node. Flush EVERY ring pod so
        # the owner is forced onto the on-disk SAI path regardless of which node owns it.
        logger.info("[AutoCassandra20238] Flushing repro20238_ks.tbl on all ring nodes (on-disk SAI path)")
        self._flush_all()

        logger.info("[AutoCassandra20238] Firing SAI SELECT on the stabilized, flushed ring (expect buggy (0 rows))")
        sai_out = self._select_with_retries(pod, self._SAI_SELECT)
        logger.warning(
            "[AutoCassandra20238] DOCUMENTED SIGNATURE (buggy SAI result) for %r:\n%s",
            self._SAI_SELECT,
            sai_out.strip() if sai_out else "<no output>",
        )

        # Corroborate that the row is physically on disk (so the SAI (0 rows) is a wrong-result/
        # missing-row bug, not data loss). This read also retries past transient ring errors so the
        # proof reliably lands in the log rather than racing a still-settling node.
        logger.info("[AutoCassandra20238] Plain (non-SAI) read to prove the row is physically on disk")
        plain_out = self._select_with_retries(pod, self._PLAIN_SELECT)
        logger.warning(
            "[AutoCassandra20238] Corroborating plain read %r (expect the surviving row 0|1|1|null|1):\n%s",
            self._PLAIN_SELECT,
            plain_out.strip() if plain_out else "<no output>",
        )

        logger.info("[AutoCassandra20238] Deploying SELECT-only continuous reproducer")
        self.app.deploy_continuous_reproducer(self._SAI_SELECT, self.expected_output)

    # ── Ring stabilization & verification ───────────────────────────────────────

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
        exec'ing/querying it then yields NoHostAvailable — so skip any pod with a deletionTimestamp.
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
        logger.info("[AutoCassandra20238] Scaling operators up; waiting for ring to re-stabilize (<=%ds)", timeout)
        try:
            self.app._scale_operator_up()
        except Exception as e:
            logger.warning("[AutoCassandra20238] operator scale-up raised: %s", e)

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
                    "[AutoCassandra20238] ring check: UN=%d total=%d schema_versions=%d",
                    un,
                    len(node_lines),
                    len(schemas),
                )
                if un >= 3 and un == len(node_lines) and len(schemas) == 1:
                    logger.info("[AutoCassandra20238] Ring stable (3x UN, single schema version)")
                    return True
            time.sleep(20)
        logger.warning("[AutoCassandra20238] Ring did not fully stabilize within %ds — proceeding best-effort", timeout)
        return False

    def _flush_all(self):
        """Run `nodetool flush repro20238_ks tbl` on EVERY ring pod (RF=1: only the owner holds the
        partition, so flush all to force the owner onto the on-disk SAI path)."""
        pods = self._ring_server_pods()
        if not pods:
            logger.warning("[AutoCassandra20238] No Cassandra pods found — skipping nodetool flush")
            return
        for pod in pods:
            rc, _, err = self._exec_in_pod(pod, ["nodetool", "flush", "repro20238_ks", "tbl"], quiet=True)
            if rc == 0:
                logger.info("[AutoCassandra20238] nodetool flush ok on pod %s", pod)
            else:
                logger.warning("[AutoCassandra20238] nodetool flush on %s exited %s: %s", pod, rc, err.strip()[:200])

    def _select_with_retries(self, pod: str, cql: str, retries: int = 12, sleep: int = 15) -> str:
        """Run a SELECT via the server pod's cqlsh, retrying past spurious ring-restart errors.

        Returns the first cqlsh output that contains a definitive ``(N rows)`` result line (the
        documented buggy ``(0 rows)`` for the SAI query, or the surviving row for the plain read);
        if every attempt only yields transient ring-restart noise, returns the last such output."""
        last = ""
        for attempt in range(retries):
            rc, out, err = self._run_cql_in_pod(pod, cql, quiet=True)
            combined = f"{out}\n{err}"
            if "rows)" in out:
                return out
            if rc == 0 and not self._SPURIOUS.search(combined):
                return out
            logger.info(
                "[AutoCassandra20238] SELECT hit spurious ring error (attempt %d/%d), retrying",
                attempt + 1,
                retries,
            )
            np = self._ready_ring_pod()
            if np:
                pod = np
            last = (out or err).strip()
            time.sleep(sleep)
        return last

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
        if not username or not password:
            return "", ""
        return base64.b64decode(username).decode(), base64.b64decode(password).decode()

    def _run_cql_in_pod(self, pod: str, cql: str, quiet: bool = False) -> tuple[int, str, str]:
        """Run CQL via cqlsh inside the server pod (K8ssandra requires auth). Returns
        (returncode, stdout, stderr)."""
        username, password = self._get_cql_credentials()
        auth = ""
        if username and password:
            u_b64 = base64.b64encode(username.encode()).decode()
            p_b64 = base64.b64encode(password.encode()).decode()
            auth = f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            cqlsh = 'cqlsh -u "$U" -p "$P" --request-timeout=60'
        else:
            cqlsh = "cqlsh --request-timeout=60"
        result = subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- bash -c '{auth}{cqlsh}'",
            shell=True,
            capture_output=True,
            text=True,
            input=cql,
        )
        if not quiet:
            if result.stdout.strip():
                logger.info("[AutoCassandra20238] cqlsh stdout: %s", result.stdout.strip()[:400])
            if result.returncode != 0:
                logger.warning(
                    "[AutoCassandra20238] cqlsh exited %s: %s", result.returncode, result.stderr.strip()[:400]
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
                logger.info("[AutoCassandra20238] `%s` stdout: %s", joined, result.stdout.strip()[:400])
            if result.returncode != 0:
                logger.warning(
                    "[AutoCassandra20238] `%s` exited %s: %s", joined, result.returncode, result.stderr.strip()[:400]
                )
        return result.returncode, result.stdout, result.stderr
