"""CASSANDRA-16259: nodetool tablehistograms throws ArrayIndexOutOfBoundsException after an
in-place 3.11.8 -> 3.11.9 upgrade.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16259
Buggy: 3.11.9   ->   Fixed: 3.11.10
Component: Observability/Metrics

Reproduction summary (an UPGRADE scenario — NOT a single fresh node):
  A table that holds BOTH an sstable written by 3.11.8 (115 cell-count histogram bucket rows =
  114 offsets + overflow) and an sstable written by 3.11.9 (119 rows = 118 + overflow, because
  CASSANDRA-15164 raised the CellPerPartitionCount default from 114->118) makes
  TableMetrics.combineHistograms size its accumulator from the larger array, then index the
  smaller sstable's bucket array out of bounds the first time the histograms are combined. A
  fresh single-version 3.11.9 node CANNOT reproduce this (verified): within one version every
  sstable's cell-count histogram has the same bucket count, so the mismatch only arises across
  the 3.11.8->3.11.9 boundary. Triggered via `nodetool tablehistograms repro16259_ks hist_bug`.

How it is realised on the raw-ring infra (a true in-place upgrade on ONE data directory):
  The single deployed Cassandra version cannot, by itself, produce two sstables with mismatched
  histogram bucket counts. ``inject_fault`` therefore stands up ONE pod whose *initContainer* runs
  cassandra:3.11.8 — it creates the table and flushes the 115-bucket sstable onto a shared
  ``emptyDir`` data directory, then drains and exits — after which the pod's *main* container boots
  cassandra:3.11.9 on the SAME data directory (the in-place upgrade), writes a second row and
  flushes the 119-bucket sstable. The two mismatched sstables now coexist on one node exactly as in
  a real 3.11.8->3.11.9 upgrade, and ``nodetool tablehistograms`` throws. The CassandraLogGrepOracle
  greps the nodetool output for the verbatim exception.

Verbatim buggy signature (from the reproduction evidence log):
  error: 115
  -- StackTrace --
  java.lang.ArrayIndexOutOfBoundsException: 115
      at org.apache.cassandra.metrics.TableMetrics.combineHistograms(TableMetrics.java:261)
      at org.apache.cassandra.metrics.TableMetrics.access$000(TableMetrics.java:48)
      at org.apache.cassandra.metrics.TableMetrics$11.getValue(TableMetrics.java:376)
      at org.apache.cassandra.metrics.TableMetrics$11.getValue(TableMetrics.java:373)
  command terminated with exit code 2

The fixed image cassandra:3.11.10 on the IDENTICAL upgraded data (same md-1=115, md-2=119
sstables) returns the histogram table with EXIT 0 — no exception.
"""

import logging
import re
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_UPGRADE_POD = "cassup"
_OLD_IMAGE = "cassandra:3.11.8"  # writes the 115-bucket (3.11.8) sstable in the initContainer
_KS = "repro16259_ks"
_TABLE = f"{_KS}.hist_bug"
# nodetool exits 2 and prints the stack trace to stderr; capture it and never fail the exec.
_TABLEHISTOGRAMS_CMD = f"nodetool tablehistograms {_KS} hist_bug 2>&1 || true"
_AIOOBE = r"ArrayIndexOutOfBoundsException: \d+"

# Pod manifest for the in-place cross-version upgrade on a single emptyDir data dir.
# Placeholders (__NS__/__MAIN_IMAGE__/__OLD_IMAGE__) are substituted via str.replace so the
# literal CQL braces in the init script are left untouched.
_POD_TEMPLATE = """
apiVersion: v1
kind: Pod
metadata:
  name: cassup
  namespace: __NS__
  labels: { app: cassup }
spec:
  terminationGracePeriodSeconds: 20
  initContainers:
    - name: write-old-3118
      image: __OLD_IMAGE__
      imagePullPolicy: IfNotPresent
      env:
        - { name: CASSANDRA_CLUSTER_NAME, value: "upgr16259" }
        - { name: MAX_HEAP_SIZE, value: "512M" }
        - { name: HEAP_NEWSIZE, value: "128M" }
      command: ["bash","-c"]
      args:
        - |
          set +e
          echo "[init] booting cassandra 3.11.8 ..."
          docker-entrypoint.sh cassandra > /var/lib/cassandra/init88.log 2>&1 &
          CPID=$!
          ok=0
          for i in $(seq 1 72); do
            if cqlsh -e 'SELECT now() FROM system.local' >/dev/null 2>&1; then ok=1; break; fi
            sleep 5
          done
          if [ "$ok" != 1 ]; then echo "[init] 3.11.8 CQL never up"; tail -60 /var/lib/cassandra/init88.log; exit 1; fi
          cqlsh -e "CREATE KEYSPACE IF NOT EXISTS repro16259_ks WITH replication={'class':'SimpleStrategy','replication_factor':1};"
          cqlsh -e "CREATE TABLE IF NOT EXISTS repro16259_ks.hist_bug (pk int, ck int, v text, PRIMARY KEY (pk, ck)) WITH compaction={'class':'SizeTieredCompactionStrategy','enabled':'false'};"
          cqlsh -e "INSERT INTO repro16259_ks.hist_bug (pk,ck,v) VALUES (1,0,'old8a'); INSERT INTO repro16259_ks.hist_bug (pk,ck,v) VALUES (2,0,'old8b'); INSERT INTO repro16259_ks.hist_bug (pk,ck,v) VALUES (3,0,'old8c');"
          nodetool disableautocompaction repro16259_ks hist_bug
          nodetool flush repro16259_ks hist_bug
          sleep 2
          echo "[init] sstables after 3.11.8 flush:"; ls -1 /var/lib/cassandra/data/repro16259_ks/hist_bug-*/ | grep Data.db || true
          nodetool drain
          sleep 2
          nodetool stopdaemon 2>/dev/null || true
          kill "$CPID" 2>/dev/null || true
          sleep 3
          echo "[init] DONE — 3.11.8 wrote the 115-bucket sstable"
          exit 0
      volumeMounts:
        - { name: data, mountPath: /var/lib/cassandra }
  containers:
    - name: cassandra
      image: __MAIN_IMAGE__
      imagePullPolicy: IfNotPresent
      env:
        - { name: CASSANDRA_CLUSTER_NAME, value: "upgr16259" }
        - { name: MAX_HEAP_SIZE, value: "512M" }
        - { name: HEAP_NEWSIZE, value: "128M" }
      command: ["bash","-c"]
      args:
        - |
          exec docker-entrypoint.sh cassandra -f
      readinessProbe:
        exec: { command: ["bash","-c","cqlsh -e 'SELECT now() FROM system.local' >/dev/null 2>&1"] }
        initialDelaySeconds: 20
        periodSeconds: 8
        failureThreshold: 40
      volumeMounts:
        - { name: data, mountPath: /var/lib/cassandra }
  volumes:
    - { name: data, emptyDir: {} }
"""


class AutoCassandra16259(CassandraRawRingProblem):
    """tablehistograms AIOOBE after an in-place 3.11.8 -> 3.11.9 upgrade on one data dir."""

    db_name = "cassandra"
    db_version = "3.11.9"
    cassandra_version = "3.11.9"
    source_git_ref = "cassandra-3.11.9"
    ring_namespace = "cassraw-16259"
    # No StatefulSet ring node: this bug runs on a single bespoke upgrade pod created in
    # inject_fault (initContainer 3.11.8 -> main 3.11.9 on a shared emptyDir). deploy() then only
    # creates the namespace + headless Service.
    replicas = 0

    root_cause_file = "src/java/org/apache/cassandra/metrics/TableMetrics.java"
    root_cause_description = (
        "nodetool tablehistograms throws ArrayIndexOutOfBoundsException after an in-place "
        "3.11.8 -> 3.11.9 upgrade. TableMetrics.combineHistograms (line 261) aggregates the "
        "per-sstable estimatedColumnCount (cells-per-partition) EstimatedHistogram: it sizes the "
        "accumulator values[] from the FIRST sstable's bucket array, then for a later sstable with "
        "FEWER buckets runs `for (i=0; i<values.length; i++) values[i] += nextBucket[i]`, indexing "
        "nextBucket[i] out of bounds. CASSANDRA-15164 (shipped in 3.11.9) raised the default "
        "CellPerPartitionCount histogram bucket count 114->118, so a 3.11.8-written sstable "
        "(115 bucket rows) and a 3.11.9-written sstable (119 bucket rows) coexisting on the same "
        "table make combineHistograms throw ArrayIndexOutOfBoundsException: 115. Component: "
        "Observability/Metrics. Fixed in 3.11.10 / 4.0."
    )

    def _upgrade_pod_manifest(self) -> str:
        return (
            _POD_TEMPLATE.replace("__NS__", self.app.namespace)
            .replace("__MAIN_IMAGE__", self.image)
            .replace("__OLD_IMAGE__", _OLD_IMAGE)
        )

    @mark_fault_injected
    def inject_fault(self):
        """Create the cross-version upgrade pod, write the 3.11.9 sstable, and trigger the AIOOBE."""
        import subprocess

        app = self.app

        # 1. Apply the upgrade pod: init (3.11.8) writes the 115-bucket sstable onto the shared
        #    emptyDir; main (3.11.9) then boots on that same data dir (the in-place upgrade).
        logger.info(f"[16259] applying cross-version upgrade pod {_UPGRADE_POD} (init 3.11.8 -> main 3.11.9)")
        subprocess.run(
            "kubectl apply -f -",
            input=self._upgrade_pod_manifest(),
            shell=True,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if not app.wait_pod_ready(_UPGRADE_POD, timeout=480):
            logger.warning(f"[16259] {_UPGRADE_POD} did not become Ready in time; continuing")
        logger.info(
            f"[16259] release_version on {_UPGRADE_POD}: "
            f"{app.cqlsh(_UPGRADE_POD, 'SELECT release_version FROM system.local;').strip()}"
        )

        # 2. 3.11.9 phase: add a NEW-format (119-bucket) sstable alongside the 3.11.8 (115) one.
        app.nodetool(_UPGRADE_POD, f"disableautocompaction {_KS} hist_bug")
        app.cqlsh(
            _UPGRADE_POD,
            f"INSERT INTO {_TABLE} (pk,ck,v) VALUES (10,0,'new9'); "
            f"INSERT INTO {_TABLE} (pk,ck,v) VALUES (11,0,'new9b');",
        )
        app.flush(_UPGRADE_POD, f"{_KS} hist_bug")
        time.sleep(2)
        sstables = app.exec(_UPGRADE_POD, f"ls -1 /var/lib/cassandra/data/{_KS}/hist_bug-*/ | grep Data.db || true")
        logger.info(f"[16259] coexisting sstables (expect md-1=115 + md-2=119 buckets):\n{sstables.strip()}")

        # 3. Trigger: nodetool tablehistograms over the mismatched-bucket sstables -> AIOOBE.
        out = app.exec(_UPGRADE_POD, _TABLEHISTOGRAMS_CMD)
        first = "\n".join(out.splitlines()[:8])
        logger.info(f"[16259] inject_fault tablehistograms output (head):\n{first}")
        if re.search(_AIOOBE, out):
            logger.info("[16259] CAPTURED buggy signature: ArrayIndexOutOfBoundsException from combineHistograms")
        else:
            logger.warning("[16259] AIOOBE not present yet; the mitigation oracle will re-trigger tablehistograms")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=_UPGRADE_POD,
            pattern=_AIOOBE,
            source="command",
            command=_TABLEHISTOGRAMS_CMD,
            attempts=4,
            retry_delay=5.0,
        )
