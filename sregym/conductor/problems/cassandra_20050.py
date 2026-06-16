"""CASSANDRA-20050: INSERT fails with InvalidRequest for UDT clustering keys in DESC order.

Bug: When a table is created with a frozen<UDT> clustering column in DESC order
(CLUSTERING ORDER BY (ck DESC)), every INSERT that provides a UDT literal fails:

    InvalidRequest: Invalid user type literal for ck of type frozen<point>

The same INSERT succeeds on an identical table with CLUSTERING ORDER BY (ck ASC).

Root cause: UserTypes.java — when a clustering column has DESC order, Cassandra
wraps its type in a ReversedType. Before casting to UserType, the code must call
.unwrap() to strip that wrapper.  Without it, the cast fails type checking during
CQL literal validation and the INSERT is rejected.

Three locations in UserTypes.java perform this cast without unwrapping:
  Line 44:  column.type  → column.type.unwrap()
  Line 135: receiver.type → receiver.type.unwrap()
  Line 164: type cast before UserType

A dependent writer Deployment retries the INSERT every 10 s.  Every attempt
returns InvalidRequest → the container exits 1 → CrashLoopBackOff.

Affected versions: 4.0 through 4.0.14, 4.1 through 4.1.7 (fixed in 4.0.15, 4.1.8)
JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20050
"""

import logging
import subprocess

from sregym.conductor.problems.cassandra_bug import CassandraBugProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Schema that creates the broken state: frozen<UDT> as a DESC clustering key.
# The same schema with ASC works correctly — this is a purely ordering-driven bug.
_SETUP_CQL = """
    CREATE KEYSPACE IF NOT EXISTS udt_ks
        WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};

    USE udt_ks;

    CREATE TYPE IF NOT EXISTS point (x int, y int);

    CREATE TABLE IF NOT EXISTS events (
        id   INT,
        loc  frozen<point>,
        val  TEXT,
        PRIMARY KEY (id, loc)
    ) WITH CLUSTERING ORDER BY (loc DESC);
"""

# Every execution of this INSERT returns:
#   InvalidRequest: Invalid user type literal for loc of type frozen<point>
_INSERT_CQL = "INSERT INTO udt_ks.events (id, loc, val) VALUES (1, {x: 10, y: 20}, 'data');"


class Cassandra20050(CassandraBugProblem):
    """CASSANDRA-20050: DESC-ordered frozen<UDT> clustering key rejects every INSERT.

    Fault injection:
      1. Creates a keyspace, UDT, and a table with the UDT as a DESC clustering key.
      2. Deploys a writer Deployment that retries the failing INSERT every 10 s.
         Each attempt returns InvalidRequest → the container exits 1 → CrashLoopBackOff.

    The Cassandra cluster stays healthy.  The failure is visible at the application
    layer: the writer pod never succeeds.  The agent must trace the InvalidRequest
    back to the missing .unwrap() call in UserTypes.java.
    """

    cassandra_version = "4.1.7"
    source_git_ref = "cassandra-4.1.7"

    root_cause_file = "src/java/org/apache/cassandra/cql3/UserTypes.java"
    root_cause_description = (
        "INSERT with a UDT literal into a table whose frozen<UDT> clustering column "
        "uses DESC ordering (CLUSTERING ORDER BY (loc DESC)) always fails with "
        "InvalidRequest: 'Invalid user type literal for loc of type frozen<point>'. "
        "Root cause: when clustering order is DESC, Cassandra wraps the column type "
        "in ReversedType.  UserTypes.java casts the column type directly to UserType "
        "without first calling .unwrap() to strip the ReversedType wrapper (lines 44, "
        "135, 164).  The cast fails type validation, so every UDT literal in an INSERT "
        "is rejected.  The same INSERT on an identical ASC-ordered table succeeds. "
        "Fix: call .unwrap() on the type before casting to UserType in UserTypes.java."
    )

    trigger_cql = _SETUP_CQL

    @mark_fault_injected
    def inject_fault(self):
        """Create the broken schema and deploy a writer that crashes on every INSERT."""
        logger.info("[Cassandra20050] Creating UDT keyspace, type, and DESC-clustered table")
        self.app.run_cql(_SETUP_CQL)

        logger.info("[Cassandra20050] Deploying writer — will CrashLoopBackOff on InvalidRequest")
        self._deploy_writer()

    def _deploy_writer(self):
        cass_host = (
            f"{self.app.cluster_name}-{self.app.datacenter_name}-service"
            f".{self.namespace}.svc.cluster.local"
        )
        secret_name = f"{self.app.cluster_name}-superuser"

        manifest = f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cassandra-writer
  namespace: {self.namespace}
  labels:
    app: cassandra-writer
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cassandra-writer
  template:
    metadata:
      labels:
        app: cassandra-writer
    spec:
      containers:
      - name: writer
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
          echo "Connected. Starting write loop against {cass_host}"
          while true; do
            output=$(cqlsh {cass_host} -u "$CASS_USER" -p "$CASS_PASS" \\
              -e "{_INSERT_CQL}" 2>&1)
            code=$?
            echo "$output"
            if [ $code -ne 0 ]; then
              echo "WRITE FAILED (exit $code)"
              exit 1
            fi
            echo "Write OK — sleeping 10s"
            sleep 10
          done
"""
        result = subprocess.run(
            "kubectl apply -f -",
            shell=True, input=manifest, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(f"[Cassandra20050] Writer deploy failed: {result.stderr.strip()}")
        else:
            logger.info("[Cassandra20050] Writer deployed — expect CrashLoopBackOff on InvalidRequest")

    @mark_fault_injected
    def recover_fault(self):
        subprocess.run(
            f"kubectl delete deployment cassandra-writer -n {self.namespace} --ignore-not-found",
            shell=True, check=False,
        )
        logger.info("[Cassandra20050] Writer deleted")
