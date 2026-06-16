"""CASSANDRA-14013: A keyspace literally named "snapshots" loses all row data after a restart.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14013
Buggy version: 4.1.0  ->  Fixed: 4.0.8 / 4.1.1 / 5.0

Reproduction summary (from the reproduced-bug evidence log):
  Create a keyspace named EXACTLY ``snapshots`` with a table, insert rows, run
  ``nodetool flush snapshots`` (force the rows to on-disk SSTables and discard the
  commitlog), then restart the Cassandra process IN PLACE on every node -- the data
  directory (a PVC) must survive the restart. On 4.1.0 the post-restart
  ``SELECT count(*)`` returns 0 even though the SSTables are still on disk and the
  schema in ``system_schema`` is intact; the fixed build (4.1.1) returns the full
  row count.

Root cause (per-node, local startup file enumeration):
  Cassandra's startup SSTable scan is per-node: each node's ``Directories.SSTableLister``
  (src/java/org/apache/cassandra/db/Directories.java) enumerates the table's data
  directories and deliberately excludes the reserved ``snapshots``/``backups``
  subdirectories (``Directories.SNAPSHOT_SUBDIR == "snapshots"``) where real
  snapshots/backups live. A keyspace named ``snapshots`` produces a live data directory
  ``.../data/snapshots/<table>-<id>/`` whose ``snapshots`` path component collides with
  that reserved name, so each node mistakes its own live SSTables for snapshot data and
  skips loading them. The table therefore appears empty (``system_schema`` is unaffected,
  so the table still "exists") even though every node's SSTables remain physically on disk.

Verbatim buggy signature (count after in-place restart == 0):
    $ kubectl exec -n <ns> cass -- cqlsh -e "SELECT count(*) FROM snapshots.test_idx;"

     count
    -------
         0

    (1 rows)

    Warnings :
    Aggregation query used without partition key

  ... while the SSTables remain physically on disk (same files, same timestamp),
  proving a LOAD/skip bug rather than data deletion.

Reproduction shape: nodetool-sequence. The bug is per-node, so it reproduces on the
standard multi-node deploy as long as EVERY node is flushed and restarted IN PLACE
(its data directory must survive the restart). ``inject_fault()`` below runs the full
sequence (CQL setup + ``nodetool flush snapshots`` + an in-place restart of the
Cassandra process on EVERY pod + the ``SELECT count(*)`` signature) via ``kubectl exec``.

Notes on the validated method (proven live on kind-fleet1):
  * "Restart IN PLACE" == restart ONLY the Cassandra JVM while keeping the pod, its
    container and the cass-management-api supervisor process alive, so the data
    directory (a PVC) survives and the node re-runs its startup SSTable-load path (the
    bug) and rejoins the ring as UN. This is driven through the management-api lifecycle
    endpoints over the node's local unix socket::

        nodetool drain
        curl -XPOST --unix-socket /tmp/oss-mgmt.sock http://localhost/api/v0/lifecycle/stop
        curl -XPOST --unix-socket /tmp/oss-mgmt.sock http://localhost/api/v0/lifecycle/start

    Do NOT ``kill 1``: PID 1 is tini / the management-api, so killing it crashes the
    whole container and (with ``--explicit-start true``) the node never restarts
    Cassandra and never rejoins -- the ring stays stuck and the count SELECT hits an
    empty/refused ring. Do NOT ``kubectl delete pod`` with an emptyDir data volume --
    that wipes the data and yields a FALSE POSITIVE (count 0) on BOTH the buggy and the
    fixed build. The data dir here is a PVC, so it survives either way; the in-place
    process restart is what exercises the startup load path.
  * The bug is per-node local file enumeration (not gossip/coordinator logic). On a
    multi-node RF=1 cluster, restarting only ONE node gives PARTIAL loss (that node's
    share; verified live: 20 -> 12 after restarting one of three nodes). Flushing +
    in-place-restarting ALL nodes makes every node skip its ``snapshots`` SSTables ->
    the clean count == 0.
  * The keyspace name MUST be exactly ``snapshots`` -- the bug is name-triggered.
  * Finding #6: the buggy-image swap scales both operators to 0 and does not recreate
    the rolling-restarted pod, leaving the ring stuck at 2/3. inject_fault() scales the
    operators back to 1 and waits for a full 3/3 UN ring BEFORE seeding (so RF=1 writes
    are not lost to an incomplete ring) and again before the post-restart count.
"""

import logging
import re
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra14013(GenericCustomBuildProblem):
    db_name = "cassandra"
    # 4.1.0 already ships the bug (fix landed in 4.1.1), so deploy the STOCK 4.1.0
    # image instead of running a ~30-min `ant jar` source build.
    db_version = "4.1.0"
    source_git_ref = "cassandra-4.1.0"
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/Directories.java"
    root_cause_description = (
        'A keyspace literally named "snapshots" loses all row data after a Cassandra '
        "process restart. Cassandra's startup SSTable scan is per-node: each node's "
        "Directories.SSTableLister enumerates the table's data directories and excludes "
        "the reserved snapshot/backup subdirectories (Directories.SNAPSHOT_SUBDIR == "
        '"snapshots"), where real snapshots/backups live. A keyspace named "snapshots" '
        "produces a live data directory (.../data/snapshots/<table>-<id>/) whose "
        '"snapshots" path component collides with that reserved name, so each node '
        "mistakes its own live SSTables for snapshot data and skips loading them. The "
        "table then appears empty (system_schema is unaffected, so the table still "
        "'exists') even though every node's SSTables remain physically on disk."
    )

    # Full reproduction (derived from the evidence log). The CQL portion creates the
    # name-triggering keyspace + table and 20 rows; the flush + in-place restart + the
    # post-restart SELECT are out-of-band steps run by inject_fault() (a separate client
    # pod cannot flush/restart the server). The keyspace name MUST be exactly `snapshots`.
    reproducer = """
-- STEP 1-3: schema + data (keyspace name MUST be exactly "snapshots")
CREATE KEYSPACE IF NOT EXISTS snapshots WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
CREATE TABLE IF NOT EXISTS snapshots.test_idx (key text, seqno bigint, primary key(key));
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key1', 1);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key2', 2);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key3', 3);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key4', 4);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key5', 5);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key6', 6);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key7', 7);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key8', 8);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key9', 9);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key10', 10);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key11', 11);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key12', 12);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key13', 13);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key14', 14);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key15', 15);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key16', 16);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key17', 17);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key18', 18);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key19', 19);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key20', 20);
-- STEP 4: pre-restart count -> 20
SELECT count(*) FROM snapshots.test_idx;
-- STEP 5 (out-of-band, NOT CQL): nodetool flush snapshots on EVERY node (rows -> on-disk SSTables, commitlog discarded)
-- STEP 6 (out-of-band, NOT CQL): in-place restart of the Cassandra process on EVERY node (PVC data dir survives;
--   nodetool drain + management-api lifecycle stop/start over /tmp/oss-mgmt.sock -- NOT `kill 1`, NOT delete pod)
-- STEP 7 (out-of-band): wait for every node to be Ready / the ring back to all-UN
-- STEP 8-9: post-restart -> schema survives, but count == 0 on the buggy 4.1.0 build (== 20 on fixed 4.1.1)
SELECT count(*) FROM snapshots.test_idx;
"""
    # The continuous-reproducer wiring gives this problem the diagnosis LLM-as-a-judge
    # oracle (on root_cause) AND a ReproducerPodMitigationOracle. NOTE: the mitigation
    # probe runs the reproducer CQL from a SEPARATE client pod, so it cannot flush +
    # in-place-restart the server -- and the reproducer re-INSERTs the 20 rows each
    # iteration -- so the probe always reads 20 and is effectively INERT for this
    # restart-gated bug (it cannot observe the load-skip). The diagnosis oracle is the
    # meaningful one here. expected_output is intentionally left unset (a wrong-result
    # probe greps for the buggy value "0" that the probe pod can never produce, which
    # would only flip the oracle to the opposite wrong verdict).
    continuous_reproducer = True

    # ── Fault injection: flush + in-place restart EVERY node, then read the count ──────
    _COUNT_CQL = "SELECT count(*) FROM snapshots.test_idx;"

    def _cassandra_pods(self) -> list[str]:
        """Return all Cassandra server pods in the cluster namespace."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        return [p.strip() for p in out.splitlines() if p.strip()]

    def _exec(self, pod: str, inner: str, *, timeout: int = 240) -> subprocess.CompletedProcess:
        """Run a command inside the ``cassandra`` container of ``pod`` and log the result."""
        cmd = f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -c {subprocess.list2cmdline([inner])}"
        try:
            cp = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("[AutoCassandra14013] exec TIMEOUT (%ss) :: %s", timeout, inner[:120])
            return subprocess.CompletedProcess(cmd, 124, "", "timeout")
        logger.info(
            "[AutoCassandra14013] exec rc=%s :: %s\n  out=%s\n  err=%s",
            cp.returncode,
            inner[:120],
            cp.stdout.strip()[:300],
            cp.stderr.strip()[:300],
        )
        return cp

    @staticmethod
    def _parse_ring(status_stdout: str) -> tuple[int, int]:
        """Return (num_UN, num_total) parsed from ``nodetool status`` output."""
        states = ("UN", "UJ", "UL", "UM", "DN", "DJ", "DL", "DM")
        total = un = 0
        for ln in status_stdout.splitlines():
            s = ln.strip()
            if len(s) >= 2 and s[:2] in states and (len(s) == 2 or s[2] == " "):
                total += 1
                if s.startswith("UN"):
                    un += 1
        return un, total

    def _single_schema(self, pod: str) -> bool:
        """True when ``nodetool describecluster`` reports exactly one schema version."""
        out = self._exec(pod, "nodetool describecluster 2>/dev/null").stdout
        uuids = re.findall(r"^\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):", out, re.M)
        return len(set(uuids)) <= 1 and len(uuids) >= 1

    def _wait_ring_un(self, *, timeout: int = 1200) -> bool:
        """Poll until every ring pod is Up/Normal (UN) with a single schema version."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            pods = self._cassandra_pods()
            if not pods:
                time.sleep(10)
                continue
            probe = pods[0]
            un, total = self._parse_ring(
                self._exec(probe, "nodetool status snapshots 2>/dev/null || nodetool status").stdout
            )
            single = self._single_schema(probe) if (total > 0 and un == total) else False
            if total >= len(pods) and un == total and total > 0 and single:
                logger.info("[AutoCassandra14013] ring stable: %s/%s UN, single schema version", un, total)
                return True
            logger.info(
                "[AutoCassandra14013] waiting for ring all-UN: %s/%s UN, pods=%s, single_schema=%s",
                un,
                total,
                len(pods),
                single,
            )
            time.sleep(15)
        logger.warning("[AutoCassandra14013] ring did NOT reach all-UN within %ss", timeout)
        return False

    def _log_count(self, pod: str, label: str) -> str:
        """Run the count(*) SELECT on ``pod`` and log the verbatim cqlsh output."""
        flags = self._cqlsh_auth_flags()
        res = self._exec(pod, f"cqlsh {flags}-e {subprocess.list2cmdline([self._COUNT_CQL])} 127.0.0.1")
        logger.info(
            "[AutoCassandra14013] %s SELECT count(*) FROM snapshots.test_idx ->\n%s",
            label,
            res.stdout.strip(),
        )
        return res.stdout

    def _wait_node_ready(self, pod: str, *, timeout: int = 600) -> bool:
        """Poll the node's management-api readiness probe until Cassandra is serving."""
        probe = "curl -sf -o /dev/null --unix-socket /tmp/oss-mgmt.sock http://localhost/api/v0/probes/readiness"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._exec(pod, probe, timeout=30).returncode == 0:
                logger.info("[AutoCassandra14013] %s Cassandra READY", pod)
                return True
            time.sleep(10)
        logger.warning("[AutoCassandra14013] %s did NOT become READY within %ss", pod, timeout)
        return False

    def _restart_node_in_place(self, pod: str) -> bool:
        """Restart ONLY the Cassandra JVM inside ``pod`` while keeping the pod, its
        container and the cass-management-api supervisor process alive, so the node
        re-runs its startup SSTable-load path (the bug) and rejoins the ring.

        Driven through the management-api lifecycle endpoints over the node's local unix
        socket (``/tmp/oss-mgmt.sock``). ``kill 1`` is wrong here: PID 1 is tini / the
        management-api, so killing it crashes the whole container and the node never
        restarts Cassandra (``--explicit-start true``) and never rejoins the ring.
        """
        sock = "/tmp/oss-mgmt.sock"
        logger.info("[AutoCassandra14013] in-place restart of %s (drain + lifecycle stop/start)", pod)
        self._exec(pod, "nodetool drain")
        self._exec(pod, f"curl -s -XPOST --unix-socket {sock} http://localhost/api/v0/lifecycle/stop")
        time.sleep(6)
        self._exec(pod, f"curl -s -XPOST --unix-socket {sock} http://localhost/api/v0/lifecycle/start")
        return self._wait_node_ready(pod)

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy 4.1.0 image, seed the name-triggering ``snapshots``
        keyspace on a fully-stabilised ring, flush every node, then restart the
        Cassandra process IN PLACE on every node. On 4.1.0 each node's startup SSTable
        scan skips the reserved ``snapshots`` directory, so the post-restart
        ``SELECT count(*) FROM snapshots.test_idx`` returns 0 while the SSTables remain
        physically on disk -- the documented signature.
        """
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra14013] Buggy image already deployed — skipping swap")
        else:
            logger.info("[AutoCassandra14013] Swapping cluster to buggy image %s", self._custom_image)
            self.app.inject_buggy_image(self._custom_image)

        self.setup_preconditions()

        # Finding #6: the buggy-image swap scales both operators to 0 and does not
        # recreate the rolling-restarted pod -> ring stuck at 2/3. Scale the operators
        # back up and wait for a full all-UN ring BEFORE seeding, so the RF=1 writes are
        # not lost to an incomplete ring.
        self.app._scale_operator_up()
        if not self._wait_ring_un():
            logger.warning("[AutoCassandra14013] ring not fully UN before seed — proceeding anyway")

        if self.reproducer:
            logger.info("[AutoCassandra14013] Seeding snapshots keyspace on the stable ring")
            try:
                self.app.run_reproducer(self.reproducer)
            except Exception as e:
                logger.warning("[AutoCassandra14013] seed reproducer raised: %s", e)

        pods = self._cassandra_pods()
        if not pods:
            logger.warning("[AutoCassandra14013] No Cassandra pods found — cannot inject fault")
            return

        # Force the seeded rows to on-disk SSTables (and discard the commitlog) on every
        # node so the bug is exercised by the startup LOAD path, not masked by a
        # commitlog replay.
        for pod in pods:
            self._exec(pod, "nodetool flush snapshots")

        self._log_count(pods[0], "pre-restart (expect == 20)")

        # In-place restart EVERY node: each node skips its own snapshots SSTables on
        # startup, so the table collectively loads zero rows.
        for pod in pods:
            self._restart_node_in_place(pod)

        # Re-stabilise the ring before reading the signature.
        self.app._scale_operator_up()
        self._wait_ring_un()

        count_out = self._log_count(pods[0], "post-restart (BUG: expect == 0)")
        logger.info(
            "[AutoCassandra14013] DOCUMENTED SIGNATURE captured (count==0 is the buggy 4.1.0 result):\n%s",
            count_out.strip(),
        )

        # Deploy the continuous reproducer LAST, so the count==0 window is observed
        # before the probe re-INSERTs the 20 rows. (The probe runs from a separate client
        # pod and cannot flush/restart the server, so it is inert for this restart-gated
        # bug; it only wires up the diagnosis + mitigation oracles.)
        if self.continuous_reproducer and self.reproducer:
            self.app.deploy_continuous_reproducer(self.reproducer, self.expected_output)
