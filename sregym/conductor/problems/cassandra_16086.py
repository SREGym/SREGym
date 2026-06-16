"""CASSANDRA-16086: Tombstone-heavy SELECT causes persistent query failures.

Bug: tombstone_failure_threshold is configured far below its safe default (100 instead
of 100,000).  Any SELECT that sweeps a partition with more than 100 tombstones raises
TombstoneOverwhelmingException on the coordinator.  All such reads fail; the cluster
appears healthy but is effectively unreadable for tombstone-heavy workloads.

Root cause: ReadCommand.java — the tombstone counter is compared against
DatabaseDescriptor.getTombstoneFailureThreshold() after every row is processed.

Affected versions: 3.x through 4.x
Configuration: tombstone_failure_threshold (set to 100; safe default is 100,000)
JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16086
"""

import base64 as _b64
import logging
import subprocess

from sregym.conductor.problems.cassandra_bug import CassandraBugProblem
from sregym.service.apps.cassandra import Cassandra
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Threshold configured dangerously low (production default is 100,000)
_TOMBSTONE_THRESHOLD = 100

# 150 INSERTs + 150 DELETEs — exceeds the threshold of 100.
# Each row deletion leaves a tombstone the subsequent SELECT must scan through.
_INSERTS = "\n        ".join(
    f"INSERT INTO heavy_deletes (pk, ck, v) VALUES (1, {i}, 'data');"
    for i in range(150)
)
_DELETES = "\n        ".join(
    f"DELETE FROM heavy_deletes WHERE pk = 1 AND ck = {i};"
    for i in range(150)
)

_TRIGGER_CQL = f"""
        CREATE KEYSPACE IF NOT EXISTS tombstone_ks
            WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': 1}};

        USE tombstone_ks;

        CREATE TABLE IF NOT EXISTS heavy_deletes (
            pk  int,
            ck  int,
            v   text,
            PRIMARY KEY (pk, ck)
        );

        {_INSERTS}

        {_DELETES}

        SELECT * FROM heavy_deletes WHERE pk = 1;
"""


class _CassandraWithLowTombstoneThreshold(Cassandra):
    """Cassandra deployment with tombstone_failure_threshold set dangerously low.

    Prometheus metrics are enabled so the agent can observe the
    tombstoneFailures counter (cassandra_table_tombstone_failures_total)
    incrementing on the metrics endpoint (port 9103).
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
    telemetry:
      prometheus:
        enabled: true
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
          cassandraYaml:
            tombstone_warn_threshold: 50
            tombstone_failure_threshold: {_TOMBSTONE_THRESHOLD}
"""


class Cassandra16086(CassandraBugProblem):
    """CASSANDRA-16086: Tombstone-heavy SELECT causes persistent read failures.

    Deploys Cassandra 4.1.7 with tombstone_failure_threshold: 100 (default 100,000).
    The trigger CQL creates 150 tombstones then runs a SELECT that trips the threshold,
    raising TombstoneOverwhelmingException.  A background loop keeps firing the failing
    SELECT every 15 s so the error is continuously visible in:
      - Cassandra system logs (TombstoneOverwhelmingException)
      - Prometheus metrics (cassandra_table_tombstone_failures_total, port 9103)

    The root cause is visible in ReadCommand.java at the tombstone counter check.
    """

    cassandra_version = "4.1.7"
    source_git_ref = "cassandra-4.1.7"
    allows_rebuild = True

    root_cause_file = "src/java/org/apache/cassandra/db/ReadCommand.java"
    root_cause_description = (
        "tombstone_failure_threshold misconfigured to 100 (default is 100,000) causes "
        "TombstoneOverwhelmingException on every SELECT that scans more than 100 tombstones. "
        "In ReadCommand.java the tombstone counter is compared against "
        "DatabaseDescriptor.getTombstoneFailureThreshold() after each row; exceeding the "
        "threshold aborts the read and returns a read failure to the client. With 150 row "
        "tombstones present in the partition (from DELETE operations) all reads against that "
        "partition fail. Fix: raise tombstone_failure_threshold to 100,000 (the safe default)."
    )

    trigger_cql = _TRIGGER_CQL

    def _create_app(self) -> Cassandra:
        return _CassandraWithLowTombstoneThreshold(cassandra_version=self.cassandra_version)

    @mark_fault_injected
    def inject_fault(self):
        """Create 150 tombstones, deploy a reader Deployment that crashes on the
        failing SELECT (→ CrashLoopBackOff), and start an in-pod background loop
        so TombstoneOverwhelmingException appears continuously in logs and the
        tombstoneFailures Prometheus counter keeps incrementing.
        """
        logger.info("[Cassandra16086] Running trigger CQL to create tombstones")
        try:
            result = self.app.run_cql(self.trigger_cql)
            logger.info(f"[Cassandra16086] CQL trigger completed: {result!r}")
        except Exception as e:
            # The final SELECT raises TombstoneOverwhelmingException — expected.
            logger.info(f"[Cassandra16086] Expected tombstone error from trigger SELECT: {e}")

        logger.info("[Cassandra16086] Deploying reader Deployment (will enter CrashLoopBackOff)")
        self._deploy_reader_workload()

        logger.info("[Cassandra16086] Starting background query loop")
        self._start_background_workload()

    def _deploy_reader_workload(self):
        """Deploy a Deployment whose sole job is to SELECT from the tombstone-heavy
        partition.  The SELECT immediately raises TombstoneOverwhelmingException,
        the container exits 1, and Kubernetes restarts it → CrashLoopBackOff.

        Credentials are injected via secretKeyRef so the pod spec never contains
        plaintext passwords.  The Cassandra native-transport service name follows
        the K8ssandra convention: {cluster}-{datacenter}-service.
        """
        cass_host = (
            f"{self.app.cluster_name}-{self.app.datacenter_name}-service"
            f".{self.namespace}.svc.cluster.local"
        )
        secret_name = f"{self.app.cluster_name}-superuser"
        select_cql = "SELECT * FROM tombstone_ks.heavy_deletes WHERE pk = 1;"

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
              -e "{select_cql}" 2>&1)
            code=$?
            echo "$output"
            if [ $code -ne 0 ]; then
              echo "READ FAILED (exit $code)"
              exit 1
            fi
            echo "Read OK — sleeping 15s"
            sleep 15
          done
"""
        result = subprocess.run(
            "kubectl apply -f -",
            shell=True, input=manifest, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(f"[Cassandra16086] Reader Deployment apply failed: {result.stderr.strip()}")
        else:
            logger.info("[Cassandra16086] Reader Deployment deployed — expect CrashLoopBackOff")

    def _start_background_workload(self):
        """Fire the failing SELECT every 15 s inside a kubectl exec loop.

        Each iteration increments cassandra_table_tombstone_failures_total on the
        Prometheus metrics endpoint (port 9103) and logs TombstoneOverwhelmingException
        in the Cassandra system log.
        """
        pod = subprocess.run(
            f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")

        if not pod:
            logger.warning("[Cassandra16086] No Cassandra pod found — skipping background workload")
            return

        username, password = self.app._get_cql_credentials()
        u_b64 = _b64.b64encode(username.encode()).decode()
        p_b64 = _b64.b64encode(password.encode()).decode()
        select_cql = "SELECT * FROM tombstone_ks.heavy_deletes WHERE pk = 1;"

        cmd = (
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f"while true; do "
            f"cqlsh -u \"$U\" -p \"$P\" -e \"{select_cql}\" 2>&1; "
            f"sleep 15; "
            f"done'"
        )
        self._workload_proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"[Cassandra16086] Background workload started on pod {pod}")

    @mark_fault_injected
    def recover_fault(self):
        """Stop the background query loop and delete the reader Deployment."""
        proc = getattr(self, "_workload_proc", None)
        if proc is not None:
            proc.terminate()
            self._workload_proc = None
            logger.info("[Cassandra16086] Background workload stopped")

        subprocess.run(
            f"kubectl delete deployment cassandra-reader -n {self.namespace} --ignore-not-found",
            shell=True, check=False,
        )
        logger.info("[Cassandra16086] Reader Deployment deleted")
