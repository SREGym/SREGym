"""CASSANDRA-18108: Schema alteration sequence corrupts SSTable serialization header.

Bug: After the sequence CREATE TABLE → INSERT → ALTER TABLE DROP non-PK column →
ALTER TABLE RENAME PK column to the same name as the dropped column, the SSTable
serialization header records the renamed primary key column as a regular column.
SerializationHeader.Component.toHeader() passes this corrupt entry to
PartitionColumns$Builder.add().  On the next read from the flushed SSTable, the
deserialization context is built from the corrupt PartitionColumns — attempts to
read rows produce a server-side error (NullPointerException or deserialization
failure) returned to any client.

A dependent reader Deployment queries the affected table every 10 s.  Once the
memtable is flushed to the corrupt SSTable, reads fail and the reader pod exits 1
→ CrashLoopBackOff.  The Cassandra pods themselves remain running; the failure is
observable at the application layer.

Root cause visible in:
  - SerializationHeader.java:340 — toHeader() adds the renamed PK column to the
    regular-column list, violating the PartitionColumns invariant
  - PartitionColumns.java:161 — Builder.add() (assertion only fires with -ea, but
    the corrupt PartitionColumns causes downstream deserialization errors regardless)

Affected versions: 3.11.17, 4.0.12, 4.1.4+ (including 4.1.7; unfixed as of 2026)
JIRA: https://issues.apache.org/jira/browse/CASSANDRA-18108
"""

import logging
import subprocess

from sregym.conductor.problems.cassandra_bug import CassandraBugProblem
from sregym.service.apps.cassandra import Cassandra
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Schema alteration sequence that corrupts the SSTable serialization header.
#   1. CREATE TABLE with pk=c1, regular cols c2 (TEXT) and c3 (INT)
#   2. INSERT one row (c1=2, c2='val') — written to the memtable
#   3. DROP c2 — removed from live schema; memtable row still carries the value
#   4. RENAME c1 TO c2 — PK column takes the name of the just-dropped regular col
# When nodetool flush writes the memtable, the serialization header incorrectly
# records the renamed PK c2 as a regular (non-key) column.
_TRIGGER_CQL = """
    CREATE KEYSPACE IF NOT EXISTS crash_ks
        WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};

    USE crash_ks;

    CREATE TABLE IF NOT EXISTS tb (
        c1 INT,
        c3 INT,
        c2 TEXT,
        PRIMARY KEY (c1)
    ) WITH speculative_retry = 'ALWAYS';

    INSERT INTO tb (c1, c2, c3) VALUES (2, 'val', 99);

    ALTER TABLE tb DROP c2;

    ALTER TABLE tb RENAME c1 TO c2;
"""

# The SELECT that the reader pod fires to validate data.
# After the flush, Cassandra tries to read from the corrupt SSTable and returns
# a server-side error, causing the reader to exit 1 → CrashLoopBackOff.
_READ_CQL = "SELECT * FROM crash_ks.tb WHERE c2 = 2;"


class _CassandraWithAssertions(Cassandra):
    """Cassandra deployment with JVM assertions enabled (-ea).

    Required for CASSANDRA-18108: the corrupt PartitionColumns built from the
    SSTable header only surfaces as an AssertionError at Columns.java:367
    (assert !s.kind.isPrimaryKeyKind()) when -ea is active.  Without it the
    corrupt entry is silently ignored and reads succeed.
    """

    def _build_cluster_manifest(self) -> str:
        return f"""\
apiVersion: k8ssandra.io/v1alpha1
kind: K8ssandraCluster
metadata:
  name: {self.cluster_name}
  namespace: {self.namespace}
spec:
  cassandra:
    serverVersion: "{self.cassandra_version}"
    datacenters:
      - metadata:
          name: {self.datacenter_name}
        size: {self.cluster_size}
        storageConfig:
          cassandraDataVolumeClaimSpec:
            storageClassName: openebs-hostpath
            accessModes:
              - ReadWriteOnce
            resources:
              requests:
                storage: 5Gi
        resources:
          requests:
            memory: 1Gi
            cpu: 500m
          limits:
            memory: 2Gi
            cpu: "1"
        config:
          jvmOptions:
            heapSize: 512M
            vm_enable_assertions: true
"""


class Cassandra18108(CassandraBugProblem):
    """CASSANDRA-18108: DDL sequence corrupts SSTable header → dependent reader CrashLoopBackOff.

    Fault injection:
      1. Runs the DDL sequence that corrupts the SSTable serialization header.
      2. Runs nodetool flush to write the corrupt SSTable to disk.
      3. Deploys a reader Deployment that queries crash_ks.tb every 10 s and
         exits 1 on any server error or missing data → CrashLoopBackOff.

    The Cassandra pods stay running.  The agent observes a crashing application
    pod and must trace the failure back to the SSTable corruption in the source.
    """

    cassandra_version = "4.1.7"
    source_git_ref = "cassandra-4.1.7"
    allows_rebuild = True

    root_cause_file = "src/java/org/apache/cassandra/db/SerializationHeader.java"
    root_cause_description = (
        "DDL sequence (CREATE TABLE → INSERT → ALTER DROP column → ALTER RENAME PK to dropped name) "
        "causes SerializationHeader.Component.toHeader() (SerializationHeader.java:340) to record "
        "the renamed primary key column c2 as a regular (non-key) column in the SSTable "
        "serialization header.  When nodetool flush writes the memtable, this corrupt header is "
        "persisted to disk.  On the next read from the flushed SSTable, the deserialization context "
        "built from the corrupt PartitionColumns causes a server-side error returned to any client "
        "querying crash_ks.tb.  Fix: ensure toHeader() skips primary key columns when building the "
        "regular-column serialization header, so the PK column is never added to PartitionColumns."
    )

    trigger_cql = _TRIGGER_CQL

    def _create_app(self) -> Cassandra:
        return _CassandraWithAssertions(cassandra_version=self.cassandra_version)

    @mark_fault_injected
    def inject_fault(self):
        """Corrupt the SSTable header, flush it to disk, then deploy a reader that crashes on read errors.

        The Cassandra cluster stays up.  Once the memtable is flushed, any read
        against crash_ks.tb hits the corrupt SSTable and returns a server error.
        The reader Deployment exits 1 on each failure → CrashLoopBackOff.
        """
        logger.info("[Cassandra18108] Running trigger DDL to corrupt SSTable serialization header")
        result = self.app.run_cql(self.trigger_cql)
        logger.info(f"[Cassandra18108] DDL trigger completed: {result!r}")

        logger.info("[Cassandra18108] Flushing crash_ks memtable → SSTable (persists the corrupt header)")
        self._run_nodetool("flush crash_ks")

        logger.info("[Cassandra18108] Deploying reader — will enter CrashLoopBackOff on read errors")
        self._deploy_reader()

    def _deploy_reader(self):
        """Deploy a Deployment whose pod queries crash_ks.tb and exits 1 on failure.

        The pod runs cqlsh in a validation loop:
          - Runs SELECT * FROM crash_ks.tb WHERE c2 = 2
          - Exits 1 if cqlsh returns a non-zero exit code (server error)
          - Exits 1 if the result contains zero rows (data missing or corrupt)
        Kubernetes restarts the pod → same failure → CrashLoopBackOff.

        Credentials are injected via secretKeyRef so no plaintext passwords appear
        in the manifest or pod spec.
        """
        cass_host = (
            f"{self.app.cluster_name}-{self.app.datacenter_name}-service"
            f".{self.namespace}.svc.cluster.local"
        )
        secret_name = f"{self.app.cluster_name}-superuser"

        manifest = f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cassandra-reader
  namespace: {self.namespace}
  labels:
    app: cassandra-reader
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cassandra-reader
  template:
    metadata:
      labels:
        app: cassandra-reader
    spec:
      containers:
      - name: reader
        image: cassandra:{self.cassandra_version}
        env:
        - name: CASS_USER
          valueFrom:
            secretKeyRef:
              name: {secret_name}
              key: username
        - name: CASS_PASS
          valueFrom:
            secretKeyRef:
              name: {secret_name}
              key: password
        command:
        - /bin/bash
        - -c
        - |
          echo "Waiting for Cassandra to be reachable..."
          until cqlsh {cass_host} -u "$CASS_USER" -p "$CASS_PASS" \\
              -e "SELECT now() FROM system.local" > /dev/null 2>&1; do
            echo "Cannot connect — retrying in 10s"
            sleep 10
          done
          echo "Connected. Starting read validation loop against {cass_host}"
          while true; do
            output=$(cqlsh {cass_host} -u "$CASS_USER" -p "$CASS_PASS" \\
              -e "{_READ_CQL}" 2>&1)
            code=$?
            echo "$output"
            if [ $code -ne 0 ]; then
              echo "READ FAILED (exit $code)"
              exit 1
            fi
            if ! echo "$output" | grep -q "(1 rows)"; then
              echo "READ RETURNED NO DATA — expected row with c2=2, got: $output"
              exit 1
            fi
            echo "Read OK — sleeping 10s"
            sleep 10
          done
"""
        result = subprocess.run(
            "kubectl apply -f -",
            shell=True, input=manifest, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(f"[Cassandra18108] Reader Deployment apply failed: {result.stderr.strip()}")
        else:
            logger.info("[Cassandra18108] Reader Deployment deployed — expect CrashLoopBackOff once SSTable reads fail")

    def _get_cassandra_pod(self) -> str:
        result = subprocess.run(
            f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        )
        return result.stdout.strip().strip("'")

    def _run_nodetool(self, command: str):
        pod = self._get_cassandra_pod()
        if not pod:
            logger.warning(f"[Cassandra18108] No Cassandra pod found — skipping nodetool {command}")
            return
        result = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- nodetool {command}",
            shell=True, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(f"[Cassandra18108] nodetool {command} stderr: {result.stderr.strip()}")
        else:
            logger.info(f"[Cassandra18108] nodetool {command} ok: {result.stdout.strip()}")

    @mark_fault_injected
    def recover_fault(self):
        """Delete the reader Deployment. SSTable corruption persists until PVC is deleted."""
        subprocess.run(
            f"kubectl delete deployment cassandra-reader -n {self.namespace} --ignore-not-found",
            shell=True, check=False,
        )
        logger.info("[Cassandra18108] Reader Deployment deleted")
        logger.info(
            "[Cassandra18108] Note: SSTable corruption persists on disk. "
            "Full recovery requires deleting crash_ks or the PVC and redeploying."
        )
