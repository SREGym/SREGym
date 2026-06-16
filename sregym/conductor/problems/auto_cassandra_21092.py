"""CASSANDRA-21092: zero-copy streaming of legacy (pre-4.0) sstables fails with AssertionError.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21092
Buggy: 5.0.6  ->  Fixed: 5.0.7  (control: 5.0.8)

Reproduced on the raw-ring infrastructure as a CROSS-VERSION problem: the buggy
``cassandra:5.0.6`` node is the single StatefulSet ring member ``cass-0``; ``inject_fault``
additionally stands up an isolated, self-seeded ``cassandra:3.11.19`` *producer* pod (a custom
raw manifest — the infra ``extra_pods`` all use the problem's own 5.0.6 image, so the legacy
producer cannot be an extra_pod). The producer writes a table to disk so the sstables carry the
legacy (pre-4.0) bloom-filter format (``me-1-big-*`` files), those files are copied into cass-0,
and ``sstableloader`` streams them into cass-0 with zero-copy streaming
(``stream_entire_sstables=true``, the default). On buggy 5.0.6 the receiving node fails to
finalize the streamed sstable and logs the verbatim assertion below to its ``system.log``.

Reproduction (buggy, cross-version):
  1. On a cassandra:3.11.19 pod: CREATE keyspace ks21092 + table tbl, INSERT ~500 rows,
     then ``nodetool flush`` to produce me-1-big-* sstables (legacy bloom-filter format).
  2. Copy those sstable files into the cassandra:5.0.6 node (cass-0) under /staging/ks21092/tbl.
  3. On cass-0: ``sstableloader -d <cass-0-ip> /staging/ks21092/tbl``
     (default stream_entire_sstables=true / zero-copy). The receiving node asserts.

Root cause: src/java/org/apache/cassandra/utils/BloomFilterSerializer.java — the zero-copy
stream path attempts to serialize a pre-4.0 bloom filter in the old on-disk format and asserts.
The fix (5.0.7) auto-disables zero-copy streaming for sstables that carry a pre-4.0 (legacy)
bloom filter.

VERBATIM BUGGY SIGNATURE (5.0.6, cass-0 system.log; wrapped in CorruptSSTableException):
  org.apache.cassandra.io.sstable.CorruptSSTableException: Corrupted: .../ks21092/tbl-<id>/me-1-big
  Caused by: java.lang.AssertionError: Filter should not be serialized in old format
    at org.apache.cassandra.utils.BloomFilterSerializer.serialize(BloomFilterSerializer.java:52)
    at org.apache.cassandra.utils.BloomFilter.serialize(BloomFilter.java:67)
    at org.apache.cassandra.io.sstable.format.FilterComponent.save(FilterComponent.java:78)

Control (fixed 5.0.8): the identical sstables load successfully (500 rows, 0 AssertionErrors).

The ``CassandraLogGrepOracle`` greps cass-0's system.log for the verbatim
``Filter should not be serialized in old format`` line; on fixed binaries the legacy sstables
stream successfully and the line never appears.
"""

import logging
import shlex
import subprocess
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KS = "ks21092"
_TBL = "tbl"
_NROWS = 500
_PRODUCER = "cass-producer"
_PRODUCER_IMAGE = "cassandra:3.11.19"
_STAGING = f"/staging/{_KS}/{_TBL}"
_SIGNATURE = r"Filter should not be serialized in old format"
_PATH_FIX = "echo 'export PATH=/opt/cassandra/bin:/opt/cassandra/tools/bin:$PATH' > /etc/profile.d/cass.sh"

# Identical schema on the legacy producer and the 5.0.6 target (sstableloader needs the table
# to exist on the target and infers ks/table from the last two staging-path components).
_DDL = (
    f"CREATE KEYSPACE IF NOT EXISTS {_KS} WITH REPLICATION = "
    "{'class':'SimpleStrategy','replication_factor':1}; "
    f"CREATE TABLE IF NOT EXISTS {_KS}.{_TBL} (id int PRIMARY KEY, val text);"
)


class AutoCassandra21092(CassandraRawRingProblem):
    """Zero-copy streaming of legacy (pre-4.0) sstables asserts on a stock 5.0.6 node.

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:5.0.6`` single-node ring (cass-0); ``inject_fault`` deploys an isolated
    ``cassandra:3.11.19`` producer that writes legacy-format (me-1-big-*) sstables, copies them
    into cass-0, and ``sstableloader``-streams them with zero-copy (stream_entire_sstables=true);
    the ``CassandraLogGrepOracle`` greps cass-0's system.log for the verbatim
    ``Filter should not be serialized in old format`` assertion the zero-copy path raises on 5.0.6
    (gone on fixed 5.0.7+).
    """

    db_name = "cassandra"
    db_version = "5.0.6"
    cassandra_version = "5.0.6"
    source_git_ref = "cassandra-5.0.6"
    ring_namespace = "cassraw-21092"
    # The buggy 5.0.6 node is the only ring member; the legacy producer is a separate,
    # self-seeded single-node cluster deployed by inject_fault.
    replicas = 1

    root_cause_file = "src/java/org/apache/cassandra/utils/BloomFilterSerializer.java"
    root_cause_description = (
        "Zero-copy streaming (stream_entire_sstables=true) of legacy pre-4.0 sstables fails with "
        "'java.lang.AssertionError: Filter should not be serialized in old format' at "
        "BloomFilterSerializer.serialize (wrapped in CorruptSSTableException). When an sstable that "
        "carries a pre-4.0 (legacy) bloom-filter component is streamed entire/zero-copy, the receiving "
        "node's FilterComponent.save -> BloomFilter.serialize -> BloomFilterSerializer.serialize path "
        "asserts because the old on-disk bloom-filter format must not be re-serialized. The fix (5.0.7) "
        "auto-disables zero-copy streaming for sstables that carry a legacy (pre-4.0) bloom filter so "
        "they are streamed through the normal (non-zero-copy) path instead."
    )

    # ── infra helpers ───────────────────────────────────────────────────────────

    def _producer_manifest(self) -> str:
        ns = self.app.namespace
        return f"""
apiVersion: v1
kind: Pod
metadata:
  name: {_PRODUCER}
  namespace: {ns}
  labels: {{ app: cass-producer, role: producer }}
spec:
  terminationGracePeriodSeconds: 5
  containers:
    - name: cassandra
      image: {_PRODUCER_IMAGE}
      imagePullPolicy: IfNotPresent
      env:
        - {{ name: CASSANDRA_CLUSTER_NAME, value: "producer31119" }}
        - {{ name: CASSANDRA_ENDPOINT_SNITCH, value: "GossipingPropertyFileSnitch" }}
        - {{ name: MAX_HEAP_SIZE, value: "1024M" }}
        - {{ name: HEAP_NEWSIZE, value: "200M" }}
      volumeMounts:
        - {{ name: data, mountPath: /var/lib/cassandra }}
  volumes:
    - {{ name: data, emptyDir: {{}} }}
"""

    def _deploy_producer(self):
        subprocess.run(
            "kubectl apply -f -",
            shell=True,
            capture_output=True,
            text=True,
            input=self._producer_manifest(),
            timeout=120,
        )
        self.app.wait_pod_running(_PRODUCER, timeout=300)

    def _wait_producer_cql(self, timeout_s: int = 360) -> bool:
        for i in range(0, timeout_s, 10):
            if "3.11.19" in self.app.cqlsh(_PRODUCER, "SELECT release_version FROM system.local;", timeout=60):
                logger.info(f"[21092] producer ({_PRODUCER}) CQL-ready at t={i}s")
                return True
            time.sleep(10)
        return False

    def _generate_legacy_sstables(self):
        """Write ~500 rows on the 3.11.19 producer and flush -> legacy me-1-big-* sstables."""
        self.app.cqlsh(_PRODUCER, _DDL, timeout=120)
        inserts = " ".join(f"INSERT INTO {_KS}.{_TBL} (id,val) VALUES ({i},'data{i}');" for i in range(_NROWS))
        self.app.cqlsh(_PRODUCER, inserts, timeout=300)
        self.app.nodetool(_PRODUCER, f"flush {_KS} {_TBL}")
        listing = self.app.exec(_PRODUCER, f"ls /var/lib/cassandra/data/{_KS}/{_TBL}-*/ 2>/dev/null")
        logger.info(f"[21092] producer legacy sstables:\n{listing.strip()}")

    def _copy_sstables_to_target(self):
        """Stream the producer's me-1-big-* files into cass-0:/staging/<ks>/<tbl>/ via tar-over-exec."""
        ns = self.app.namespace
        srcdir = self.app.exec(
            _PRODUCER, f"ls -d /var/lib/cassandra/data/{_KS}/{_TBL}-*/ 2>/dev/null | head -1"
        ).strip()
        self.app.exec("cass-0", f"mkdir -p {_STAGING}")
        send = shlex.quote(f"cd {srcdir} && tar cf - me-* 2>/dev/null")
        recv = shlex.quote(f"cd {_STAGING} && tar xf - && ls")
        cmd = (
            f"kubectl exec -n {ns} {_PRODUCER} -c cassandra -- bash -lc {send} "
            f"| kubectl exec -i -n {ns} cass-0 -c cassandra -- bash -lc {recv}"
        )
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
        logger.info(f"[21092] copied legacy sstables to cass-0 {_STAGING}:\n{(out.stdout or '').strip()}")

    def _stream_zero_copy(self) -> str:
        """Run sstableloader (zero-copy default) from cass-0 against itself; returns its output."""
        # 5.0.x bash -lc login PATH drops /opt/cassandra/bin; restore before sstableloader.
        self.app.exec("cass-0", _PATH_FIX)
        ip = self.app.pod_ip("cass-0")
        out = self.app.exec("cass-0", f"sstableloader -d {ip} {_STAGING} 2>&1", timeout=300)
        logger.info(f"[21092] sstableloader output (tail):\n{out.strip()[-600:]}")
        return out

    # ── hooks ───────────────────────────────────────────────────────────────────

    def post_deploy(self):
        """Restore the cassandra tool PATH on the 5.0.6 ring node (bash -lc 5.0.x quirk)."""
        self.app.exec("cass-0", _PATH_FIX)

    @mark_fault_injected
    def inject_fault(self):
        """Generate legacy sstables on 3.11.19, copy to the 5.0.6 node, and zero-copy stream them."""
        self._deploy_producer()
        if not self._wait_producer_cql():
            logger.warning("[21092] producer never became CQL-ready; signature may not manifest")
            return
        self._generate_legacy_sstables()
        # The target needs the table to exist before sstableloader streams into it.
        self.app.cqlsh("cass-0", _DDL, timeout=120)
        self._copy_sstables_to_target()
        self._stream_zero_copy()
        # Let the receiving-side assertion land in cass-0's system.log before grading.
        for _ in range(12):
            time.sleep(10)
            if self.app.grep_log("cass-0", _SIGNATURE, source="system_log"):
                logger.info("[21092] inject_fault observed the zero-copy bloom-filter assertion on cass-0")
                return
        logger.warning("[21092] zero-copy assertion not yet observed (oracle will retry)")

    def retrigger(self):
        """Re-stream the legacy sstables so the oracle measures a fresh assertion.

        The legacy me-1-big-* files persist in cass-0's emptyDir staging dir and cass-0 stays up
        (the assertion is caught as CorruptSSTableException, not a crash), so re-running
        sstableloader safely re-emits the signature on each oracle attempt.
        """
        self._stream_zero_copy()

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod="cass-0",
            source="system_log",
            pattern=_SIGNATURE,
            retrigger=True,
            attempts=8,
            retry_delay=15.0,
        )
