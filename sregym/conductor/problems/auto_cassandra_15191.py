"""CASSANDRA-15191: disk_failure_policy=stop_paranoid is IGNORED on a
CorruptSSTableException thrown AFTER the node is up (e.g. a regular SELECT).

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-15191
Buggy version: 3.11.7  ->  Fixed: 3.0.22 / 3.11.8 / 4.0-beta2 / 4.0

Reproduction summary (from the reproduced-bug evidence log):
  Start a single Cassandra node with cassandra.yaml edited to
  ``disk_failure_policy: stop_paranoid`` (and ``disk_access_mode: standard`` so the
  LZ4 per-chunk CRC is re-validated from disk instead of being served from the mmap
  page cache). Create an LZ4-compressed table, INSERT ~2000 rows, ``nodetool flush``
  them to an on-disk SSTable, then corrupt the body of the ``*-Data.db`` file out of
  band (``dd if=/dev/urandom ... conv=notrunc``). A full-scan ``SELECT *`` then hits
  the corrupt chunk and raises a CorruptSSTableException at read time.

  On the buggy 3.11.7 build the stop_paranoid policy is IGNORED: gossip and the native
  (binary) transport stay RUNNING and the node keeps serving (a fresh ``SELECT now()``
  still succeeds) -- it merely logs the exception. On the fixed 3.11.8 build the policy
  fires: gossip and binary go NOT RUNNING (the JVM stays up for JMX investigation).

Root cause (per the JIRA body, confirmed by the evidence log):
  The exception that reaches the disk-failure-policy check is a ``RuntimeException``
  whose *cause* is the ``CorruptSSTableException`` (it is wrapped while propagating up
  through ``AbstractLocalAwareExecutorService``), so the policy check does not recognise
  it as a corrupt-sstable failure and stop_paranoid is never applied.

Verbatim buggy signature (from the evidence log):
  The trigger SELECT fails identically on BOTH builds (this is NOT the signature):
    <stdin>:1:ReadFailure: Error from server: code=1300 [Replica(s) failed to execute
    read] message="Operation failed - received 0 responses and 1 failures"
    info={'failures': 1, 'received_responses': 0, 'required_responses': 1,
    'consistency': 'ONE'}

  Server log -- ROOT-CAUSE SIGNATURE (RuntimeException wrapping CorruptSSTableException
  as its cause; frame is AbstractLocalAwareExecutorService):
    java.lang.RuntimeException: org.apache.cassandra.io.sstable.CorruptSSTableException:
        Corrupted: /var/lib/cassandra/data/repro15191/t-.../md-1-big-Data.db
      at org.apache.cassandra.service.StorageProxy$DroppableRunnable.run(StorageProxy.java:2656)
      at org.apache.cassandra.concurrent.AbstractLocalAwareExecutorService$FutureTask.run(AbstractLocalAwareExecutorService.java:165)
      at org.apache.cassandra.concurrent.AbstractLocalAwareExecutorService$LocalSessionFutureTask.run(AbstractLocalAwareExecutorService.java:137)
      ...
    Caused by: org.apache.cassandra.io.sstable.CorruptSSTableException: Corrupted: .../md-1-big-Data.db
      at org.apache.cassandra.io.sstable.format.big.BigTableScanner$KeyScanningIterator.computeNext(BigTableScanner.java:405)

  KEY EVIDENCE -- policy IGNORED on 3.11.7 (node alive AFTER the corrupt read):
    statusgossip: running ; statusbinary: running ; SELECT now() succeeds.
    Zero "Stopping gossiper" / "Stopping native transport" / DiskFailure-killer log lines.

Reproduction shape: nodetool/dd-sequence over the operator ring (executed by
``inject_fault()`` via ``kubectl exec`` — the cassandra_20108 pattern). The bug needs an
out-of-band SSTable corruption + flush, which a pure CQL ``reproducer`` string cannot
express. NOTE: the original cassandra.yaml gate (stop_paranoid + standard disk_access_mode)
and in-place restart are NOT used — on the ``cass-management-api`` image PID 1 is ``tini`` so
``kill 1`` restarts the whole container (Finding #22), and the gate is unnecessary anyway:
the documented server-log frame is raised by the compressed per-chunk CRC check regardless of
disk_failure_policy/disk_access_mode (see ``setup_preconditions`` below).

NOTE on oracles (why continuous_reproducer is False):
  The manifestation is the SERVER-side root-cause frame (a RuntimeException wrapping a
  CorruptSSTableException at BigTableScanner.java:405), captured by ``_capture_signature()``
  from each node's ``system.log``/``debug.log``. The standard ReproducerPodMitigationOracle
  cannot express it — its probe runs CQL from a separate client pod and the trigger SELECT
  raises ReadFailure identically on the buggy AND the fixed build, so a continuous reproducer
  would report "bug present" on both versions (silently broken). So this is diagnosis-only:
  continuous_reproducer = False attaches just the LLM-as-a-judge diagnosis oracle on
  root_cause, and expected_output is left None (this is an ignored-policy / server-log bug,
  NOT a wrong-result bug).
"""

import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra15191(GenericCustomBuildProblem):
    db_name = "cassandra"
    # 3.11.7 already ships the bug (fix landed in 3.11.8), so deploy the STOCK 3.11.7
    # image instead of running a ~30-min `ant jar` source build.
    db_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/concurrent/AbstractLocalAwareExecutorService.java"
    root_cause_description = (
        "When disk_failure_policy=stop_paranoid and a CorruptSSTableException is thrown "
        "AFTER the server is up (e.g. on a regular SELECT that hits a corrupt SSTable "
        "chunk), the policy is IGNORED: the node should stop gossip + the native "
        "transport but instead just logs the exception and keeps serving. The exception "
        "that reaches the disk-failure-policy check is a RuntimeException whose *cause* is "
        "the CorruptSSTableException (wrapped while propagating up through "
        "AbstractLocalAwareExecutorService), so the policy check does not recognise it as "
        "a corrupt-sstable failure and stop_paranoid is never applied."
    )

    # Full reproduction (derived from the evidence log). The CQL portion creates the
    # LZ4-compressed table and INSERTs 2000 rows; the cassandra.yaml gate, the
    # `nodetool flush`, the out-of-band `dd` corruption of the *-Data.db file, and the
    # liveness probes are out-of-band steps that a CQL-only `reproducer` string cannot
    # express -- they are run by setup_preconditions()/inject_fault() below.
    # Default LZ4 compression is kept on purpose: the per-chunk CRC is what raises
    # CorruptSSTableException once the chunk body is corrupted.
    KEYSPACE = "repro15191"
    TABLE = "t"
    # Rows of high-entropy (base64-of-urandom) payload. An all-'x' payload LZ4-compresses to
    # almost nothing, leaving a Data.db too small to corrupt a real compressed chunk; random
    # bytes keep the flushed SSTable incompressible so `dd` hits a genuine chunk whose
    # per-chunk CRC then fails on read (≈160 KiB/node for 600 rows on a 3-node ring).
    _N_ROWS = 600
    reproducer = """
-- Operator-runtime reproduction (executed by inject_fault(), NOT via the CQL-only path).
-- NOTE: no cassandra.yaml gate / restart is used. The documented server-log signature (a
--   RuntimeException wrapping a CorruptSSTableException at BigTableScanner.java:405) is raised
--   by the compressed per-chunk CRC check on ANY corrupt-SSTable scan, independent of
--   disk_failure_policy (which only governs the IGNORED downstream stop) and of
--   disk_access_mode (CompressedChunkReader validates the CRC under default mmap too).
-- STEP 1: LZ4-compressed schema (default compression kept on purpose).
CREATE KEYSPACE IF NOT EXISTS repro15191 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
CREATE TABLE IF NOT EXISTS repro15191.t (id int PRIMARY KEY, payload text);
-- STEP 2: inject_fault() bulk-loads 600 rows of high-entropy (base64-of-urandom) payload so
--   the flushed SSTable is incompressible and has a real chunk to corrupt; one row shown:
INSERT INTO repro15191.t (id, payload) VALUES (1, '<~800 random base64 chars>');
-- STEP 3 (out-of-band, NOT CQL): nodetool flush repro15191 on EVERY node (RF=1 spreads rows).
-- STEP 4 (out-of-band, NOT CQL): corrupt each node's *-Data.db body in place:
--     dd if=/dev/urandom of=<Data.db> bs=1 count=16000 seek=$((size/3)) conv=notrunc
-- STEP 5: TRIGGER -- run this full scan from EACH node's LOCAL cqlsh (127.0.0.1) so the corrupt
--   node coordinates its own LOCAL read and StorageProxy$DroppableRunnable wraps the
--   CorruptSSTableException in a RuntimeException (the documented frame):
SELECT * FROM repro15191.t;
-- STEP 6 (out-of-band): grep /var/log/cassandra/{system,debug}.log for the verbatim frame
--   (java.lang.RuntimeException: ...CorruptSSTableException ... BigTableScanner.java:405).
-- STEP 7 (out-of-band, SECONDARY): on buggy 3.11.7 gossip + binary stay RUNNING and
--   `SELECT now()` still succeeds (policy IGNORED); on fixed 3.11.8 they go NOT RUNNING.
"""
    # Diagnosis-only: the liveness signal cannot be probed by the CQL-grep mitigation
    # oracle (see the module docstring), and the trigger SELECT errors identically on both
    # builds, so we do NOT attach a (silently-broken) mitigation oracle.
    continuous_reproducer = False
    # NOT a wrong-result bug -> no expected_output (the SELECT raises ReadFailure on both
    # the buggy and the fixed build; the signature is node liveness, not a returned value).
    expected_output = None

    # ── Helpers ────────────────────────────────────────────────────────────────────────
    _DATA_DIR = "/var/lib/cassandra/data"

    def _cassandra_pods(self) -> list[str]:
        """Return all Cassandra server pods in the cluster namespace (K8ssandra label)."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        return [p.strip() for p in out.splitlines() if p.strip()]

    def _exec(self, pod: str, inner_cmd: str) -> subprocess.CompletedProcess:
        """Run a shell command inside the cassandra container of `pod`.

        Uses the argv (list) form rather than ``shell=True`` + ``{cmd!r}`` so the command —
        which mixes single and double quotes (CQL maps like ``{'class':...}``, ``tr -d '\\n'``,
        ``'$P'``) — is passed verbatim as one argument to ``bash -lc`` without the host shell
        re-parsing Python's repr (which is not POSIX-shell-safe for strings containing both
        quote kinds). Superuser auth flags are regex-injected into every ``cqlsh`` token.
        """
        inner_cmd = self._authed_cqlsh(inner_cmd)
        return subprocess.run(
            ["kubectl", "exec", "-n", self.namespace, pod, "-c", "cassandra", "--", "bash", "-lc", inner_cmd],
            capture_output=True,
            text=True,
        )

    # ── The cassandra.yaml gate is NOT required for the documented signature ─────────────
    def setup_preconditions(self):
        """No-op (documented).

        The original gate edited cassandra.yaml (``disk_failure_policy: stop_paranoid`` +
        ``disk_access_mode: standard``) and restarted the node in place via ``kill 1`` so the
        startup-read policy would apply. On the K8ssandra ``cass-management-api`` image PID 1
        is ``tini`` (cassandra is a child process — verified live), so ``kill 1`` restarts the
        WHOLE container: the operator then re-renders cassandra.yaml (dropping the edit), JMX
        (7199) is refused for minutes, and the ring frequently wedges at ``1/2`` (Finding #22).

        It is also unnecessary. The documented root-cause signature — a ``RuntimeException``
        wrapping a ``CorruptSSTableException`` (``BigTableScanner.java:405``), logged server
        side — is raised by the compressed per-chunk CRC check on ANY corrupt-SSTable scan:
          • independent of ``disk_failure_policy`` — that policy only governs whether the node
            then STOPS, which is the bug's *downstream* effect (and on buggy 3.11.7 it is
            IGNORED because the exception is wrapped), not what raises the exception; and
          • independent of ``disk_access_mode`` — ``CompressedChunkReader`` validates the CRC
            under the default mmap mode too (verified live: the frame's innermost cause is
            ``CompressedChunkReader$Mmap.readChunk``).
        So we leave the node untouched and capture the frame directly in ``inject_fault()``.
        """
        logger.info(
            "[AutoCassandra15191] setup_preconditions: no config gate / in-place restart "
            "(the server-log RuntimeException->CorruptSSTableException frame is independent of "
            "disk_failure_policy and fires under the default mmap read path)"
        )

    # ── Post-swap ring stabilization (heals the operator-override-degraded ring) ─────────
    def _expected_ring_size(self) -> int:
        """Sum the desired replica counts of the datacenter StatefulSet(s) = full ring size.

        After the buggy-image swap the live pod count is temporarily N-1 (a restarted pod
        can't be recreated while the operator/webhook is down), so we size the ring from the
        StatefulSet spec, not the current pods.
        """
        out = subprocess.run(
            f"kubectl get statefulsets -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"-o jsonpath='{{range .items[*]}}{{.spec.replicas}} {{end}}' 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        total = sum(int(x) for x in out.split() if x.strip().isdigit())
        return total or 3

    def _wait_ring_stable(self, timeout: int = 600) -> bool:
        """Heal and block until the ring is back to the FULL all-``UN`` size with native
        transport up on every node.

        The buggy-image swap is performed by ``inject_buggy_image`` → ``_operator_override``,
        which scales ALL operator Deployments to 0 and patches the StatefulSet directly. That
        leaves the ring degraded: the rolling restart takes one pod down, and with the
        operators (and their mutating ``mpod.kb.io`` pod webhook) gone the StatefulSet
        controller CANNOT recreate it (``FailedCreate ... webhook ... connection refused``),
        so the ring is stuck at N-1 nodes. A full-range scan needs EVERY token range, so a
        node-down ring makes the trigger ``SELECT`` fail with ``Unavailable``/``ReadFailure``
        on the missing range BEFORE it ever scans the corrupt SSTable (Finding #6).

        The CassandraDatacenter/K8ssandraCluster CR already carries the buggy image (the swap
        patched the CR, verified live), so scaling the operators back to 1 brings the webhook
        back, lets the missing pod be recreated, and heals the ring to all-``UN`` WITHOUT
        reverting the buggy image. Polls ``nodetool status`` until the full ring is ``UN`` and
        a superuser ``SELECT now()`` succeeds.
        """
        # Heal: scale every operator Deployment back to 1 (webhook returns; missing pod is
        # recreated with the buggy image from the CR).
        logger.info("[AutoCassandra15191] scaling operators back to 1 to heal the post-swap ring")
        try:
            self.app._scale_operator_up()
        except Exception as e:  # noqa: BLE001 - best-effort heal
            logger.warning(f"[AutoCassandra15191] _scale_operator_up raised: {e}")

        expected = self._expected_ring_size()
        deadline = time.time() + timeout
        while time.time() < deadline:
            pods = self._cassandra_pods()
            if pods:
                seed = pods[0]
                status = self._exec(seed, "nodetool status 2>/dev/null || true").stdout
                up = sum(1 for ln in status.splitlines() if ln.strip().startswith("UN"))
                native = all(
                    "running" in self._exec(p, "nodetool statusbinary 2>/dev/null || true").stdout for p in pods
                )
                if up >= expected and len(pods) >= expected and native:
                    alive = self._exec(seed, "cqlsh -e 'SELECT now() FROM system.local;' 2>&1 || true").stdout
                    if "now(" in alive or "rows)" in alive:
                        logger.info(
                            f"[AutoCassandra15191] ring stable: {up}/{expected} UN, native transport up, "
                            f"cqlsh responsive"
                        )
                        return True
            time.sleep(10)
        logger.warning(f"[AutoCassandra15191] ring did not reach {expected} UN within {timeout}s — proceeding anyway")
        return False

    # ── Surface the documented server-log signature (the manifestation) ──────────────────
    def _capture_signature(self, pods: list[str]) -> bool:
        """Grep every node's system.log/debug.log for the documented frame and log it.

        The discriminating, root-cause signature is the SERVER-side
        ``java.lang.RuntimeException: ...CorruptSSTableException ...
        StorageProxy$DroppableRunnable.run(StorageProxy.java:2656)`` /
        ``BigTableScanner$KeyScanningIterator.computeNext(BigTableScanner.java:405)`` frame —
        NOT the client ReadFailure (identical on the fixed build). Logs a ``*** MANIFESTED
        ***`` block and returns True when the frame is present on any node.
        """
        logs = "/var/log/cassandra/system.log /var/log/cassandra/debug.log"
        manifested = False
        for pod in pods:
            wrapped = self._exec(
                pod,
                "grep -A12 -E "
                "'java.lang.RuntimeException: org.apache.cassandra.io.sstable.CorruptSSTableException' "
                f"{logs} 2>/dev/null | head -30 || true",
            ).stdout.strip()
            frame = self._exec(
                pod,
                "grep -hE "
                "'CorruptSSTableException|BigTableScanner.java:405|DroppableRunnable.run.StorageProxy.java' "
                f"{logs} 2>/dev/null | head -20 || true",
            ).stdout.strip()
            if frame and "CorruptSSTableException" in frame:
                manifested = True
                logger.info(
                    f"[AutoCassandra15191] *** MANIFESTED *** CASSANDRA-15191 server-log signature "
                    f"on {pod} (stop_paranoid IGNORED; the exception reaching the policy check is a "
                    f"RuntimeException whose cause is the CorruptSSTableException):\n{wrapped or frame}"
                )
        if not manifested:
            logger.warning(
                "[AutoCassandra15191] CorruptSSTableException/BigTableScanner.java:405 frame NOT "
                "found in any node log — signature did not manifest"
            )
        return manifested

    # ── Fault injection: insert -> flush -> corrupt SSTable -> trigger -> grep server log ─
    @mark_fault_injected
    def inject_fault(self):
        """Run the full CASSANDRA-15191 sequence and surface the server-log signature.

        ``super().inject_fault()`` swaps in the buggy 3.11.7 image (a rolling restart that, via
        the operator-override, leaves the ring degraded). This method first HEALS the ring back
        to its full all-``UN`` size (``_wait_ring_stable`` scales the operators back to 1 — the
        CR already carries the buggy image, so the heal does not revert it), then bulk-loads
        incompressible rows, ``nodetool flush``es them to on-disk SSTables, corrupts each node's
        ``*-Data.db`` body with ``dd``, fires the trigger ``SELECT *`` from EACH node's LOCAL
        cqlsh, and greps the server logs for the documented
        ``RuntimeException``-wrapping-``CorruptSSTableException`` (``BigTableScanner.java:405``)
        frame.

        Firing the trigger from every node's OWN cqlsh is essential: only when the corrupt
        node is the COORDINATOR does its LOCAL read run through
        ``StorageProxy$DroppableRunnable``, which wraps the ``CorruptSSTableException`` in a
        ``RuntimeException`` — the exact documented frame. A read coordinated by a healthy
        node logs the bare exception on a remote ReadStage without that wrapper.
        """
        # Image swap + (no-op) setup_preconditions + reproducer CQL (schema).
        super().inject_fault()

        if not self._cassandra_pods():
            logger.warning("[AutoCassandra15191] No Cassandra pods found — skipping flush/corrupt steps")
            return

        # The swap leaves the ring degraded (operators scaled to 0 → a restarted node cannot be
        # recreated). Heal it to the FULL all-UN ring before triggering, otherwise the
        # full-range scan fails on the missing token range instead of hitting the corrupt SSTable.
        self._wait_ring_stable()
        pods = self._cassandra_pods()  # re-fetch: the recreated node is back now
        if not pods:
            logger.warning("[AutoCassandra15191] No Cassandra pods after ring heal — skipping")
            return
        seed = pods[0]

        # Fresh LZ4-compressed schema (DROP+CREATE on the now-healthy ring guarantees a clean
        # single SSTable to corrupt), then bulk-load incompressible rows so the flushed SSTable
        # has a real compressed body whose per-chunk CRC fails on read.
        create = (
            f'cqlsh -e "DROP KEYSPACE IF EXISTS {self.KEYSPACE}; '
            f"CREATE KEYSPACE {self.KEYSPACE} WITH replication="
            f"{{'class':'SimpleStrategy','replication_factor':1}}; "
            f"CREATE TABLE {self.KEYSPACE}.{self.TABLE} "
            f'(id int PRIMARY KEY, payload text);"'
        )
        self._exec(seed, create)

        bulk_insert = (
            f'{{ echo "USE {self.KEYSPACE};"; '
            f"for i in $(seq 1 {self._N_ROWS}); do "
            "P=$(head -c 600 /dev/urandom | base64 | tr -d '\\n' | head -c 800); "
            f"echo \"INSERT INTO {self.TABLE} (id, payload) VALUES ($i, '$P');\"; "
            "done; } | cqlsh"
        )
        logger.info(f"[AutoCassandra15191] seed={seed}: bulk-insert {self._N_ROWS} high-entropy rows")
        self._exec(seed, bulk_insert)

        # Flush so the rows land in on-disk SSTables. RF=1 spreads them across the ring, so
        # flush + corrupt EVERY node.
        for pod in pods:
            logger.info(f"[AutoCassandra15191] pod={pod}: nodetool flush {self.KEYSPACE}")
            self._exec(pod, f"nodetool flush {self.KEYSPACE}")

        # Corrupt the body of each node's *-Data.db: overwrite 16000 bytes ~1/3 into the file
        # in place (conv=notrunc keeps the size). The compressed per-chunk CRC then fails on
        # the full scan -> CorruptSSTableException.
        corrupt_cmd = (
            f"for D in $(find {self._DATA_DIR}/{self.KEYSPACE} -name '*-Data.db'); do "
            'SZ=$(stat -c %s "$D"); OFF=$((SZ/3)); '
            'dd if=/dev/urandom of="$D" bs=1 count=16000 seek=$OFF conv=notrunc status=none; '
            "done"
        )
        for pod in pods:
            logger.info(f"[AutoCassandra15191] pod={pod}: corrupt on-disk *-Data.db body via dd")
            self._exec(pod, corrupt_cmd)

        # TRIGGER from EACH node's LOCAL cqlsh so the corrupt node coordinates its own LOCAL
        # read -> StorageProxy$DroppableRunnable wraps the CorruptSSTableException in a
        # RuntimeException (the documented root-cause frame). Fails with ReadFailure on BOTH
        # builds client-side (NOT the signature).
        for pod in pods:
            logger.info(f"[AutoCassandra15191] pod={pod}: TRIGGER local-coordinator full-scan SELECT")
            self._exec(pod, f"cqlsh 127.0.0.1 -e 'SELECT * FROM {self.KEYSPACE}.{self.TABLE};' 2>&1 || true")

        # SURFACE the documented server-log signature (the manifestation).
        self._capture_signature(pods)

        # Secondary (ignored-policy) evidence: on buggy 3.11.7 gossip + binary stay RUNNING
        # and the node keeps serving after the corrupt read (the policy was IGNORED).
        gossip = self._exec(seed, "nodetool statusgossip 2>&1 || true").stdout.strip()
        binary = self._exec(seed, "nodetool statusbinary 2>&1 || true").stdout.strip()
        alive = self._exec(seed, "cqlsh -e 'SELECT now() FROM system.local;' 2>&1 || true").stdout.strip()
        logger.info(
            f"[AutoCassandra15191] post-trigger liveness (buggy 3.11.7 keeps serving = policy "
            f"IGNORED): statusgossip={gossip!r} statusbinary={binary!r} "
            f"select_now_ok={('now(' in alive or 'rows)' in alive)!r}"
        )
