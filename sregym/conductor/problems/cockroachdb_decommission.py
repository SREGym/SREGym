"""
CockroachDB Decommission Benchmark - Simulating operator's Decommission reconcile action.

This benchmark tests an agent's ability to act as a Kubernetes operator's Decommission reconciler.
The agent must safely scale down a CockroachDB cluster from 5 nodes to 3 nodes by:
1. Decommissioning nodes 4 and 3 (in that order - highest first)
2. Waiting for data to be migrated off those nodes
3. Scaling down the StatefulSet
4. Optionally cleaning up PVCs

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/decommission/
"""

import logging

from sregym.conductor.oracles.cockroachdb_decommission_oracle import CockroachDBDecommissionOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_DECOMMISSION_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBDecommissionApp:
    """
    Lightweight app class for CockroachDB Decommission benchmark.

    This app does NOT deploy any resources in deploy() - the preconditions
    (5-node initialized cluster) are set up in inject_fault() instead,
    following the incident management pattern.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-decommission-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Decommission Benchmark - operator Decommission action simulation"

    def deploy(self):
        """
        No-op deployment.

        Preconditions (5-node cluster) are set up in inject_fault() instead,
        which is called by conductor after NOOP stage and before agent starts working.
        """
        local_logger.info(f"[Decommission Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent decommissions nodes."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Decommission Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBDecommission(Problem):
    """
    CockroachDB Decommission benchmark problem.

    Simulates the operator's Decommission action which safely scales down a CockroachDB cluster
    by decommissioning nodes before removing them.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up 5-node initialized cluster and updates CR to request scale-down
    4. Agent detects scale-down request (CR spec.nodes=3, but StatefulSet has 5 replicas)
    5. Agent decommissions nodes 4 and 3, waits for completion, then scales down StatefulSet
    6. Oracle validates cluster has 3 healthy nodes and StatefulSet is scaled down
    """

    def __init__(self):
        self.app = CockroachDBDecommissionApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBDecommissionOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Act as CockroachDB operator's Decommission reconciler. Safely scale down the cluster from 5 to 3 nodes.

OBSERVED STATE:
- CrdbCluster CR spec.nodes = 3 (scaled down from 5)
- StatefulSet still has 5 replicas (crdb-cluster-0 through crdb-cluster-4)
- CR has annotation: crdb.cockroachlabs.com/decommission=scale-down-to-3
- All 5 pods are running and cluster is initialized
- Cluster currently has 5 healthy nodes with data distributed across them

DESIRED STATE:
- Only 3 nodes in cluster (nodes 0, 1, 2)
- Nodes 3 and 4 safely decommissioned (data migrated off)
- StatefulSet scaled down to 3 replicas
- Cluster healthy and functional with 3 nodes
- PVCs for decommissioned nodes cleaned up (optional)

YOUR TASK:
Safely scale down the CockroachDB cluster by decommissioning nodes before removal.

CONSTRAINTS:
- Maintain cluster quorum throughout (need N/2+1 nodes minimum)
- No data loss during decommission
- Cluster must remain functional after scale-down

CRITICAL SAFETY REQUIREMENTS:
- Nodes MUST be decommissioned in REVERSE order (highest ordinal first: 4, then 3)
- Data MUST be fully migrated off a node before scaling down StatefulSet
- StatefulSet replica count should only be reduced AFTER nodes are safely decommissioned

IMPORTANT CONCEPTS:
- CockroachDB uses distributed replication (default 3x)
- Decommissioning migrates data ranges off a node before removal
- Must wait for decommission to complete (100% - all ranges moved)
- Only scale down StatefulSet after decommission is complete

Read the CrdbCluster CR 'crdb-cluster' in namespace 'cockroachdb' for cluster configuration.
Investigate current cluster state and determine the necessary steps to achieve the desired state."""

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
        - StatefulSet with 5 replicas
        - Initializes the cluster
        - Creates CrdbCluster CR requesting scale-down to 3 nodes

        The "fault" here is the mismatch: CR wants 3 nodes, but StatefulSet has 5.
        Agent must decommission nodes before scaling down.
        """
        local_logger.info(f"\n[Decommission Benchmark] Setting up preconditions...")

        # 1. Create namespace
        local_logger.info(f"  [1/8] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            if "AlreadyExists" in result:
                local_logger.info(f"    ℹ Namespace already exists")
            else:
                local_logger.info(f"    ✓ Namespace created")
        except Exception as e:
            local_logger.info(f"    ℹ Namespace may already exist: {e}")

        # 2. Install CockroachDB CRDs
        local_logger.info(f"  [2/8] Installing CockroachDB CRDs...")
        crd_url = "https://raw.githubusercontent.com/cockroachdb/cockroach-operator/master/install/crds.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {crd_url}")
        local_logger.info(f"    ✓ CRDs installed")

        # 2.5. Delete validating webhook
        local_logger.info(f"  [2.5/8] Removing validating webhook (no operator in this benchmark)...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    ✓ Webhook removed")
        except Exception as e:
            local_logger.info(f"    ℹ Webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/8] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_DECOMMISSION_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/8] Creating Services (Discovery + Public)...")
        services_path = f"{COCKROACH_DB_DECOMMISSION_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 5. Create StatefulSet with 5 replicas
        local_logger.info(f"  [5/8] Creating StatefulSet with 5 replicas...")
        sts_path = f"{COCKROACH_DB_DECOMMISSION_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    ✓ StatefulSet created")

        # Wait for first pod to be running (not necessarily ready)
        local_logger.info(f"  [5.5/8] Waiting for first pod to be running (for initialization)...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=jsonpath='{{.status.phase}}'=Running pod/{self.cluster_name}-0 --timeout=300s"
            )
            local_logger.info(f"    ✓ Pod {self.cluster_name}-0 is running")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Pod may not be running yet: {e}")

        # Give CockroachDB a moment to start listening
        import time

        local_logger.info(f"  [5.6/8] Waiting 30 seconds for CockroachDB process to start...")
        time.sleep(30)
        local_logger.info(f"    ✓ Wait complete")

        # 6. Initialize the cluster
        local_logger.info(f"  [6/8] Initializing the 5-node cluster...")
        max_retries = 5
        for attempt in range(max_retries):
            try:
                init_cmd = f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach init --insecure"
                result = self.kubectl.exec_command(init_cmd)
                local_logger.info(f"    ✓ Cluster initialized")
                break
            except Exception as e:
                if "already initialized" in str(e).lower():
                    local_logger.info(f"    ℹ  Cluster already initialized")
                    break
                elif attempt < max_retries - 1:
                    local_logger.info(f"    ⚠️  Init attempt {attempt + 1} failed, retrying in 10s: {e}")
                    time.sleep(10)
                else:
                    local_logger.info(f"    ⚠️  Warning: Could not initialize cluster: {e}")

        # Now wait for all 5 pods to be ready (should succeed after init)
        local_logger.info(f"  [6.5/8] Waiting for all 5 pods to be ready (now that cluster is initialized)...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod -l app.kubernetes.io/name=cockroachdb --timeout=600s"
            )
            local_logger.info(f"    ✓ All 5 pods are ready")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Not all pods ready yet: {e}")

        # Verify cluster has 5 nodes
        local_logger.info(f"  [7/9] Verifying cluster has 5 healthy nodes...")
        try:
            node_status_cmd = (
                f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach node status --insecure"
            )
            result = self.kubectl.exec_command(node_status_cmd)
            local_logger.info(f"    ✓ Cluster has 5 nodes")
            # Print node status for debugging
            local_logger.info(f"\n    Initial node status:")
            for line in result.split("\n")[:7]:  # Print first few lines
                local_logger.info(f"    {line}")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Could not verify node status: {e}")

        # 8. Create CrdbCluster CR requesting scale-down to 3 nodes
        local_logger.info(f"  [8/9] Creating CrdbCluster CR requesting scale-down to 3 nodes...")
        cr_path = f"{COCKROACH_DB_DECOMMISSION_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created (spec.nodes=3)")

        # 8. Verify the mismatch (fault condition)
        local_logger.info(f"\n[Decommission Benchmark] Verifying preconditions...")
        try:
            # Check CR wants 3 nodes
            cr_cmd = f"kubectl -n {self.namespace} get crdbcluster {self.cluster_name} -o jsonpath='{{.spec.nodes}}'"
            cr_nodes = self.kubectl.exec_command(cr_cmd)
            local_logger.info(f"  ✓ CrdbCluster CR spec.nodes = {cr_nodes}")

            # Check StatefulSet has 5 replicas
            sts_cmd = f"kubectl -n {self.namespace} get sts {self.cluster_name} -o jsonpath='{{.spec.replicas}}'"
            sts_replicas = self.kubectl.exec_command(sts_cmd)
            local_logger.info(f"  ✓ StatefulSet replicas = {sts_replicas}")

            if cr_nodes.strip() != sts_replicas.strip():
                local_logger.info(
                    f"  ✓ Mismatch detected (fault injected): CR wants {cr_nodes} nodes, StatefulSet has {sts_replicas}"
                )
            else:
                local_logger.info(f"  ⚠️  Warning: No mismatch detected")

        except Exception as e:
            local_logger.info(f"  ⚠️  Warning: Could not verify fault condition: {e}")

        local_logger.info(f"\n[Decommission Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Safely scale down cluster from 5 to 3 nodes")
        local_logger.info(f"Expected actions:")
        local_logger.info(
            f"  1. Decommission node 5 (highest ID): kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach node decommission 5 --insecure"
        )
        local_logger.info(f"  2. Wait for decommission completion")
        local_logger.info(
            f"  3. Decommission node 4: kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach node decommission 4 --insecure"
        )
        local_logger.info(f"  4. Wait for decommission completion")
        local_logger.info(
            f'  5. Scale down StatefulSet: kubectl -n {self.namespace} patch sts {self.cluster_name} -p \'{{"spec":{{"replicas":3}}}}\''
        )
        local_logger.info(f"  6. Optional: Delete PVCs for removed nodes\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup() which is called by conductor
        pass
