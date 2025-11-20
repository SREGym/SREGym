"""
CockroachDB Node Drain Maintenance Benchmark - Draining a node for maintenance.

This benchmark tests an agent's ability to drain a CockroachDB node
for maintenance without decommissioning it.

The agent must:
1. Drain a node (move its replicas to other nodes)
2. Verify replicas have moved to healthy nodes
3. Ensure cluster quorum is maintained
4. Verify node is not serving traffic
5. Allow node to be undrainedfor recovery

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/node-drain-maintenance/
"""

import logging

from sregym.conductor.oracles.cockroachdb_node_drain_maintenance_oracle import CockroachDBNodeDrainMaintenanceOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_NODE_DRAIN_MAINTENANCE_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBNodeDrainMaintenanceApp:
    """Lightweight app class for CockroachDB Node Drain Maintenance benchmark."""

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-node-drain-maintenance-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Node Drain Maintenance Benchmark"

    def deploy(self):
        """No-op deployment."""
        local_logger.info(f"[Node Drain Maintenance Benchmark] App deploy() called - no action needed")

    def start_workload(self):
        """No workload to start."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Node Drain Maintenance Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBNodeDrainMaintenance(Problem):
    """CockroachDB Node Drain Maintenance benchmark problem."""

    def __init__(self):
        self.app = CockroachDBNodeDrainMaintenanceApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"
        self.target_node = 1  # Will drain node 1

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBNodeDrainMaintenanceOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Drain a node for maintenance without decommissioning it.

OBSERVED STATE:
- CockroachDB cluster with 3 healthy nodes
- All nodes serving traffic with replicas
- CR has annotation: crdb.cockroachlabs.com/drain-node=required
- Node 1 needs maintenance (hardware, OS update, etc.)
- Cluster is fully healthy with quorum

DESIRED STATE:
- Node 1 is drained (no replicas, no traffic)
- All replicas moved to other healthy nodes (nodes 0, 2)
- Cluster quorum maintained (2 nodes still available)
- Node 1 can be taken offline safely for maintenance
- Node 1 can be restored/undrainedafter maintenance

YOUR TASK:
Drain node 1 for maintenance without decommissioning it.

CONSTRAINTS:
- Maintain cluster quorum (need N/2+1 nodes = 2 out of 3)
- Move all replicas from node 1 to other nodes
- Verify no traffic served by node 1
- Ensure drain is graceful (no data loss)
- Allow undrain after maintenance

IMPORTANT CONCEPTS:
- Draining is different from decommissioning (temporary vs permanent)
- Drained node has no replicas or traffic
- Cluster remains fully functional with N-1 nodes
- Replicas are moved, not deleted

Drain the node and verify all replicas moved to other nodes."""

    @mark_fault_injected
    def inject_fault(self):
        """Set up preconditions for the benchmark."""
        local_logger.info(f"\n[Node Drain Maintenance Benchmark] Setting up preconditions...")

        # 1. Create namespace
        local_logger.info(f"  [1/7] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            local_logger.info(f"    ✓ Namespace created")
        except Exception as e:
            local_logger.info(f"    ℹ Namespace may already exist: {e}")

        # 2. Install CockroachDB CRDs
        local_logger.info(f"  [2/7] Installing CockroachDB CRDs...")
        crd_url = "https://raw.githubusercontent.com/cockroachdb/cockroach-operator/master/install/crds.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {crd_url}")
        local_logger.info(f"    ✓ CRDs installed")

        # 2.5. Delete validating webhook
        local_logger.info(f"  [2.5/7] Removing validating webhook...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    ✓ Webhook removed")
        except Exception as e:
            local_logger.info(f"    ℹ Webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/7] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_NODE_DRAIN_MAINTENANCE_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/7] Creating Services...")
        services_path = f"{COCKROACH_DB_NODE_DRAIN_MAINTENANCE_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 5. Create StatefulSet
        local_logger.info(f"  [5/7] Creating StatefulSet...")
        sts_path = f"{COCKROACH_DB_NODE_DRAIN_MAINTENANCE_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    ✓ StatefulSet created")

        # 6. Wait for cluster to be ready
        local_logger.info(f"  [6/7] Waiting for cluster to be ready...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod -l app.kubernetes.io/name=cockroachdb --timeout=600s"
            )
            local_logger.info(f"    ✓ Cluster ready")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Cluster may not be ready: {e}")

        # 7. Create CrdbCluster CR
        local_logger.info(f"  [7/7] Creating CrdbCluster CR...")
        cr_path = f"{COCKROACH_DB_NODE_DRAIN_MAINTENANCE_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created")

        local_logger.info(f"\n[Node Drain Maintenance Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Drain node {self.target_node} for maintenance\n")

    @mark_fault_injected
    def recover_fault(self):
        """Clean up resources."""
        pass
