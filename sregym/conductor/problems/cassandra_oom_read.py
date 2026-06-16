"""Cassandra OOM: unbounded diagnostic buffer in ReadCommand causes heap exhaustion.

Bug: ReadCommand.executeLocally() allocates a 4 MB byte array into a static
     List<byte[]> (queryDiagnosticBuffer) on EVERY local read.  The list has no
     eviction policy, so it grows without bound.  Even during normal startup,
     Cassandra performs internal reads (system tables, schema, gossip) which
     trigger the allocation.  The JVM heap fills up and crashes with:

         java.lang.OutOfMemoryError: Java heap space

     The bug logs warnings every 10 reads showing buffer growth:
         WARN - queryDiagnosticBuffer size: 10 entries (~40MB held)

     Kubernetes restarts the container, but internal reads immediately resume,
     the heap fills again, and the node enters CrashLoopBackOff.

Root cause: ReadCommand.java — a diagnostic snapshot buffer accumulated in a
     static field was never removed before the release build.  The allocation
     (4,194,304 bytes × every local read) is the sole source of heap growth; no
     other change to Cassandra is needed to reproduce the crash.

Fix: remove the queryDiagnosticBuffer field and its single call-site in
     executeLocally() (ReadCommand.java).
"""

import logging
import subprocess
from pathlib import Path

from sregym.conductor.oracles.code_fix_mitigation import CodeFixMitigationOracle
from sregym.conductor.problems.cassandra_custom_build import CassandraCustomBuildProblem
from sregym.service.apps.cassandra import Cassandra, CassandraWithCustomImage
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_SETUP_CQL = """
    CREATE KEYSPACE IF NOT EXISTS bench_ks
        WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 3};

    USE bench_ks;

    CREATE TABLE IF NOT EXISTS events (
        id  INT PRIMARY KEY,
        val TEXT
    );
"""

# Seed a handful of rows so reads actually hit storage paths.
_SEED_CQL = "\n".join(f"INSERT INTO bench_ks.events (id, val) VALUES ({i}, 'data_{i}');" for i in range(20))

_READ_CQL = "SELECT * FROM bench_ks.events;"


class _CassandraWithOomKill(CassandraWithCustomImage):
    """CassandraWithCustomImage that kills PID 1 on JVM OutOfMemoryError.

    The k8ssandra management API (PID 1) silently restarts the Cassandra JVM
    when it OOMs, hiding the crash from Kubernetes.  Setting
    ``-XX:OnOutOfMemoryError=kill -9 1`` causes the JVM to send SIGKILL to
    PID 1 (the management API itself) on OOM, making the container exit so
    Kubernetes sees the crash and enters CrashLoopBackOff.
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
    serverImage: "{self.custom_image}"
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
            heapSize: 256M
            additionalJvm11ServerOptions:
              - "-XX:OnOutOfMemoryError=/usr/local/bin/oom-kill-mgmt.sh"
              - "-XX:+HeapDumpOnOutOfMemoryError"
"""


class CassandraOomRead(CassandraCustomBuildProblem):
    """Cassandra crashes with OOM due to unbounded static diagnostic buffer in read path.

    Fault injection:
      Deploys Cassandra 4.1.7 built from a patched source where
      ReadCommand.executeLocally() accumulates 4 MB per read into a
      static List<byte[]> that is never cleared.

      The bug triggers on ALL reads, including internal system reads during
      startup (schema, gossip, etc.).  No external workload is needed — the
      cluster OOMs shortly after deploy.

    Observable symptoms:
      - Cassandra logs show buffer growth warnings every 10 reads:
          WARN - queryDiagnosticBuffer size: 10 entries (~40MB held)
      - Eventually: java.lang.OutOfMemoryError: Java heap space
      - Pods restart and immediately OOM again → CrashLoopBackOff

    The agent must locate the rogue static buffer in ReadCommand.java.

    Code fix workflow:
      Agent can edit /opt/source/src/java/org/apache/cassandra/db/ReadCommand.java
      to remove the queryDiagnosticBuffer field and its allocation, then call
      rebuild_cassandra to recompile and redeploy.
    """

    cassandra_version = "4.1.7"
    source_git_ref = "cassandra-4.1.7"
    patch_dir = Path(__file__).parent / "patches" / "cassandra_oom_read"
    allows_rebuild = True  # Enable agent to rebuild Cassandra after code fixes

    root_cause_file = "src/java/org/apache/cassandra/db/ReadCommand.java"
    root_cause_description = (
        "ReadCommand.executeLocally() (ReadCommand.java) allocates a 4 MB byte "
        "array into a static, unbounded List<byte[]> (queryDiagnosticBuffer) on "
        "EVERY local read execution, including internal system reads during startup.  "
        "The list has no maximum size and is never cleared.  Even without external "
        "workload, normal startup reads (schema, gossip, system tables) exhaust the "
        "heap and crash with OutOfMemoryError.  Logs show warnings before OOM: "
        "'queryDiagnosticBuffer size: N entries (~NMB held)'.  Kubernetes restarts "
        "the pod; it OOMs again immediately → CrashLoopBackOff.  Fix: remove the "
        "queryDiagnosticBuffer field and its allocation in executeLocally()."
    )

    trigger_cql = _SETUP_CQL

    def __init__(self):
        super().__init__()
        # Attach code fix verification oracle for mitigation stage
        self.mitigation_oracle = CodeFixMitigationOracle(
            problem=self,
            error_patterns=["OutOfMemoryError", "queryDiagnosticBuffer size:"],
            stability_window=120,  # Wait 2 minutes to ensure fix is stable
        )

    def _create_app(self) -> Cassandra:
        return _CassandraWithOomKill(
            cassandra_version=self.cassandra_version,
            custom_image=self._custom_image,
        )

    @mark_fault_injected
    def inject_fault(self):
        # The fault is already active: the patched ReadCommand.java allocates
        # 256KB on EVERY read, including internal system reads during startup.
        # Cassandra will OOM shortly after deploy without any external workload.
        #
        # Logs will show buffer growth warnings before OOM:
        #   WARN - queryDiagnosticBuffer size: 100 entries (~25MB held)
        #   WARN - queryDiagnosticBuffer size: 200 entries (~51MB held)
        #   ...
        #   java.lang.OutOfMemoryError: Java heap space
        logger.info("[CassandraOomRead] Fault already active — OOM will occur from internal reads during startup")

    def _deploy_reader(self):
        cass_host = f"{self.app.cluster_name}-{self.app.datacenter_name}-service.{self.namespace}.svc.cluster.local"
        secret_name = f"{self.app.cluster_name}-superuser"

        # Store the script in a ConfigMap so YAML quoting issues don't corrupt it.
        reader_script = """\
import time, os
from cassandra.cluster import Cluster
from cassandra.auth import PlainTextAuthProvider

host  = os.environ['CASS_HOST']
user  = os.environ['CASS_USER']
passw = os.environ['CASS_PASS']
auth  = PlainTextAuthProvider(username=user, password=passw)

print('Connecting to', host, flush=True)
cluster = None
while cluster is None:
    try:
        cluster = Cluster([host], auth_provider=auth, connect_timeout=30)
        session = cluster.connect()
        session.execute('SELECT now() FROM system.local')
    except Exception as e:
        print('Not ready:', e, flush=True)
        cluster = None
        time.sleep(5)

print('Connected. Hammering reads...', flush=True)
n = 0
while True:
    try:
        list(session.execute('SELECT * FROM bench_ks.events'))
        n += 1
        if n % 500 == 0:
            print(f'{n} scans', flush=True)
    except Exception as e:
        print('Error:', e, flush=True)
        time.sleep(1)
"""

        configmap = f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: cassandra-reader-script
  namespace: {self.namespace}
data:
  reader.py: |
{chr(10).join("    " + line for line in reader_script.splitlines())}
"""

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
        image: python:3.11-slim
        env:
        - name: CASS_HOST
          value: "{cass_host}"
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
        command: ["/bin/bash", "-c"]
        args: ["pip install cassandra-driver -q && python3 /reader.py"]
        volumeMounts:
        - name: script
          mountPath: /reader.py
          subPath: reader.py
      volumes:
      - name: script
        configMap:
          name: cassandra-reader-script
"""
        # Deploy ConfigMap first, then the Deployment
        for name, yaml in [("ConfigMap", configmap), ("Deployment", manifest)]:
            result = subprocess.run(
                "kubectl apply -f -",
                shell=True,
                input=yaml,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.warning(f"[CassandraOomRead] Reader {name} deploy failed: {result.stderr.strip()}")
                return
        logger.info("[CassandraOomRead] Read-loop workload deployed")

    @mark_fault_injected
    def recover_fault(self):
        subprocess.run(
            f"kubectl delete deployment cassandra-reader -n {self.namespace} --ignore-not-found"
            f" && kubectl delete configmap cassandra-reader-script -n {self.namespace} --ignore-not-found",
            shell=True,
            check=False,
        )
        logger.info("[CassandraOomRead] Reader workload deleted")
