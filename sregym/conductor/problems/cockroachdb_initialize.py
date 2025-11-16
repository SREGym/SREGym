"""
CockroachDB Initialize Benchmark - Simulating operator's Initialize reconcile action.

This benchmark tests an agent's ability to act as a Kubernetes operator's Initialize reconciler.
The agent must initialize a deployed CockroachDB cluster so it can accept SQL queries.

The cluster is already deployed (Services, StatefulSet exist) but not initialized.
The agent must execute "cockroach init" command to make the cluster operational.

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/initialize/
"""

import logging

from sregym.conductor.oracles.cockroachdb_initialize_oracle import CockroachDBInitializeOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_INITIALIZE_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBInitializeApp:
    """
    Lightweight app class for CockroachDB Initialize benchmark.

    This app does NOT deploy any resources in deploy() - the preconditions
    (RBAC + Services + StatefulSet + CR) are set up in inject_fault() instead,
    following the incident management pattern where faults are injected after NOOP stage.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-initialize-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Initialize Benchmark - operator Initialize action simulation"

    def deploy(self):
        """
        No-op deployment.

        Preconditions are set up in inject_fault() instead, which is called
        by conductor after NOOP stage and before agent starts working.
        """
        local_logger.info(f"[Initialize Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent initializes the cluster."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Initialize Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBInitialize(Problem):
    """
    CockroachDB Initialize benchmark problem.

    Simulates the operator's Initialize action which initializes a deployed CockroachDB cluster
    so it can accept SQL queries and serve traffic.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up preconditions (deploys cluster but doesn't initialize)
    4. Agent executes "cockroach init" command to initialize the cluster
    5. Oracle validates the cluster is initialized and operational
    """

    def __init__(self):
        self.app = CockroachDBInitializeApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBInitializeOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Act as CockroachDB operator's Initialize reconciler. Initialize the deployed CockroachDB cluster.

OBSERVED STATE:
- CrdbCluster CR exists with 3 nodes
- StatefulSet has 3 running pods
- Services are created and operational
- CR has annotation: crdb.cockroachlabs.com/initialize=required
- Cluster is deployed but NOT initialized (cannot accept SQL queries yet)
- status.clusterStatus is "Deployed" (not "Initialized")

DESIRED STATE:
- Cluster initialized and ready for SQL queries
- One node designated as initialization coordinator
- All nodes aware of cluster membership
- Cluster can serve client connections

YOUR TASK:
Initialize the CockroachDB cluster by executing the initialization command.

Key concepts to apply:
- CockroachDB requires explicit "cockroach init" command after deployment
- Execute from any pod: kubectl exec <pod-name> -- ./cockroach init --insecure
- Run init only once (it's idempotent - safe to run multiple times)
- After init, verify with "cockroach node status --insecure"
- This action runs AFTER Deploy, BEFORE normal operations

Verification commands:
1. Check cluster is initialized:
   kubectl -n cockroachdb exec crdb-cluster-0 -- ./cockroach node status --insecure
   (Should show all 3 nodes as alive and healthy)

2. Test SQL connection:
   kubectl -n cockroachdb exec crdb-cluster-0 -- ./cockroach sql --insecure -e "SELECT 1;"
   (Should return result without error)

3. Verify all pods are running:
   kubectl -n cockroachdb get pods
   (All 3 pods should be Running and Ready)

Read the CrdbCluster CR 'crdb-cluster' in namespace 'cockroachdb' for cluster configuration."""

    @mark_fault_injected
    def inject_fault(self):
        """
        Set up preconditions for the benchmark.

        This is called by conductor after NOOP stage, before agent starts working.

        Creates:
        - Namespace
        - CockroachDB CRDs
        - RBAC resources (ServiceAccount, Role, RoleBinding)
        - Services (Discovery + Public)
        - StatefulSet with 3 replicas
        - CrdbCluster CR with initialize annotation

        Does NOT initialize the cluster - that's the agent's job.
        """
        print(f"\n[Initialize Benchmark] Setting up preconditions...")

        # 1. Create namespace
        print(f"  [1/6] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            if "AlreadyExists" in result:
                print(f"    ℹ Namespace already exists")
            else:
                print(f"    ✓ Namespace created")
        except Exception as e:
            print(f"    ℹ Namespace may already exist: {e}")

        # 2. Install CockroachDB CRDs
        print(f"  [2/6] Installing CockroachDB CRDs...")
        crd_url = "https://raw.githubusercontent.com/cockroachdb/cockroach-operator/master/install/crds.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {crd_url}")
        print(f"    ✓ CRDs installed")

        # 2.5. Delete validating webhook (we don't have operator running)
        print(f"  [2.5/6] Removing validating webhook (no operator in this benchmark)...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            print(f"    ✓ Webhook removed")
        except Exception as e:
            print(f"    ℹ Webhook may not exist: {e}")

        # 3. Create RBAC resources
        print(f"  [3/6] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_INITIALIZE_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        print(f"    ✓ RBAC resources created")

        # 4. Create Services
        print(f"  [4/6] Creating Services (Discovery + Public)...")
        services_path = f"{COCKROACH_DB_INITIALIZE_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        print(f"    ✓ Services created")

        # 5. Create StatefulSet
        print(f"  [5/6] Creating StatefulSet with 3 replicas...")
        sts_path = f"{COCKROACH_DB_INITIALIZE_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        print(f"    ✓ StatefulSet created")

        # Wait for at least first pod to be running (cluster needs at least one pod to init)
        print(f"  [5.5/6] Waiting for first pod to be ready (this may take 2-3 minutes)...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod/{self.cluster_name}-0 --timeout=300s"
            )
            print(f"    ✓ First pod is ready")
        except Exception as e:
            print(f"    ⚠️  Warning: First pod may not be ready yet: {e}")
            print(f"    ℹ  Agent may need to wait for pods to be ready before initialization")

        # 6. Create CrdbCluster CR
        print(f"  [6/6] Creating CrdbCluster CR with initialize annotation...")
        cr_path = f"{COCKROACH_DB_INITIALIZE_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        print(f"    ✓ CrdbCluster CR created")

        # Verify initial state - cluster should NOT be initialized yet
        print(f"\n[Initialize Benchmark] Verifying preconditions...")
        try:
            # Try to run node status - should fail if not initialized
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach node status --insecure 2>&1 || true"
            )
            if "cluster has not yet been initialized" in result.lower() or "error" in result.lower():
                print(f"  ✓ Cluster is deployed but NOT initialized (expected)")
            else:
                print(f"  ⚠️  Warning: Cluster may already be initialized")
        except Exception as e:
            print(f"  ✓ Cluster not initialized yet (expected): {e}")

        print(f"\n[Initialize Benchmark] ✅ Preconditions complete!")
        print(f"\n📋 Agent task: Initialize the CockroachDB cluster")
        print(f"Expected action:")
        print(f"  1. Execute: kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach init --insecure")
        print(
            f"  2. Verify: kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach node status --insecure"
        )
        print(
            f"  3. Test SQL: kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT 1;'\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup() which is called by conductor
        pass
