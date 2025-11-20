"""
CockroachDB Quorum Loss Recovery Benchmark - Recovering from catastrophic quorum loss.

This benchmark tests an agent's ability to recover a CockroachDB cluster
from catastrophic quorum loss using unsafe recovery procedures.

WARNING: This is an emergency procedure that involves data loss risk.

The agent must:
1. Detect quorum loss condition (majority of nodes unavailable)
2. Use unsafe recovery procedure to restore cluster
3. Scale cluster back up to healthy state
4. Verify cluster integrity after recovery
5. Restore normal operations

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/quorum-loss-recovery/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_quorum_loss_recovery_oracle import CockroachDBQuorumLossRecoveryOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_QUORUM_LOSS_RECOVERY_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBQuorumLossRecoveryApp:
    """Lightweight app class for CockroachDB Quorum Loss Recovery benchmark."""

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-quorum-loss-recovery-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Quorum Loss Recovery Benchmark"

    def deploy(self):
        """No-op deployment."""
        local_logger.info(f"[Quorum Loss Recovery Benchmark] App deploy() called - no action needed")

    def start_workload(self):
        """No workload to start."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Quorum Loss Recovery Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBQuorumLossRecovery(Problem):
    """CockroachDB Quorum Loss Recovery benchmark problem."""

    def __init__(self):
        self.app = CockroachDBQuorumLossRecoveryApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBQuorumLossRecoveryOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Recover from catastrophic quorum loss using unsafe recovery.

OBSERVED STATE:
- CockroachDB cluster originally with 3 nodes
- 2 out of 3 nodes have failed (pods deleted, PVCs removed)
- Only 1 node remaining (quorum lost - need 2 out of 3)
- CR has annotation: crdb.cockroachlabs.com/quorum-recovery=required
- Cluster is non-functional and non-responsive

DESIRED STATE:
- Cluster recovered using unsafe recovery procedure
- Quorum restored (1 node functioning again)
- Cluster is responsive to queries
- All 3 nodes back online and healthy
- Normal replication resumed

YOUR TASK:
Recover the CockroachDB cluster from catastrophic quorum loss.

CONSTRAINTS:
- Use unsafe recovery (dangerous - accept data loss risks)
- Recover the remaining node to functioning state
- Restart failed nodes once quorum restored
- Verify cluster health
- Scale cluster back to 3 nodes

IMPORTANT CONCEPTS:
- Quorum requires majority of nodes (2 out of 3)
- With 1 node, cluster cannot achieve quorum
- Unsafe recovery forces a node to be the new cluster
- This procedure may lose recent data
- Only use in true emergency (production down)

WARNING: This procedure is destructive and risky.
Only use when normal recovery is impossible."""

    @mark_fault_injected
    def inject_fault(self):
        """
        Set up preconditions for the benchmark.

        Creates a cluster in quorum loss state (1 node functional, 2 failed).
        """
        local_logger.info(f"\n[Quorum Loss Recovery Benchmark] Setting up preconditions...")

        # 1. Create namespace
        local_logger.info(f"  [1/9] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            local_logger.info(f"    ✓ Namespace created")
        except Exception as e:
            local_logger.info(f"    ℹ Namespace may already exist: {e}")

        # 2. Install CockroachDB CRDs
        local_logger.info(f"  [2/9] Installing CockroachDB CRDs...")
        crd_url = "https://raw.githubusercontent.com/cockroachdb/cockroach-operator/master/install/crds.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {crd_url}")
        local_logger.info(f"    ✓ CRDs installed")

        # 2.5. Delete validating webhook
        local_logger.info(f"  [2.5/9] Removing validating webhook...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    ✓ Webhook removed")
        except Exception as e:
            local_logger.info(f"    ℹ Webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/9] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_QUORUM_LOSS_RECOVERY_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/9] Creating Services...")
        services_path = f"{COCKROACH_DB_QUORUM_LOSS_RECOVERY_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 5. Create StatefulSet
        local_logger.info(f"  [5/9] Creating StatefulSet...")
        sts_path = f"{COCKROACH_DB_QUORUM_LOSS_RECOVERY_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    ✓ StatefulSet created")

        # Wait for all pods
        local_logger.info(f"  [5.5/9] Waiting for all 3 pods to be running...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=jsonpath='{{.status.phase}}'=Running pod/{self.cluster_name}-0 --timeout=300s"
            )
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=jsonpath='{{.status.phase}}'=Running pod/{self.cluster_name}-1 --timeout=300s"
            )
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=jsonpath='{{.status.phase}}'=Running pod/{self.cluster_name}-2 --timeout=300s"
            )
            local_logger.info(f"    ✓ All 3 pods running")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Pods may not all be running: {e}")

        # Give CockroachDB time to start
        local_logger.info(f"  [5.6/9] Waiting 30 seconds for CockroachDB to start...")
        time.sleep(30)

        # 6. Initialize cluster with all 3 nodes
        local_logger.info(f"  [6/9] Initializing cluster with 3 nodes...")
        try:
            init_cmd = f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach init --insecure"
            result = self.kubectl.exec_command(init_cmd)
            local_logger.info(f"    ✓ Cluster initialized")
        except Exception as e:
            if "already initialized" not in str(e).lower():
                local_logger.info(f"    ⚠️  Warning: Could not initialize: {e}")

        # Wait for all pods to be ready
        local_logger.info(f"  [6.5/9] Waiting for all pods to be ready...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod -l app.kubernetes.io/name=cockroachdb --timeout=600s"
            )
            local_logger.info(f"    ✓ All pods ready")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Not all pods ready: {e}")

        # 7. Inject the fault - delete 2 pods and their PVCs to simulate quorum loss
        local_logger.info(f"  [7/9] Injecting quorum loss fault (deleting pods 1 and 2)...")
        try:
            # Delete pod 1
            self.kubectl.exec_command(
                f"kubectl -n {self.namespace} delete pod {self.cluster_name}-1 --force --grace-period=0"
            )
            # Delete pod 2
            self.kubectl.exec_command(
                f"kubectl -n {self.namespace} delete pod {self.cluster_name}-2 --force --grace-period=0"
            )
            # Delete their PVCs
            self.kubectl.exec_command(
                f"kubectl -n {self.namespace} delete pvc datadir-{self.cluster_name}-1 --force --grace-period=0"
            )
            self.kubectl.exec_command(
                f"kubectl -n {self.namespace} delete pvc datadir-{self.cluster_name}-2 --force --grace-period=0"
            )
            local_logger.info(f"    ✓ Quorum loss injected (pods 1 and 2 deleted)")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Could not fully inject fault: {e}")

        # 8. Create CrdbCluster CR requesting recovery
        local_logger.info(f"  [8/9] Creating CrdbCluster CR with recovery annotation...")
        cr_path = f"{COCKROACH_DB_QUORUM_LOSS_RECOVERY_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created")

        # 9. Verify quorum loss state
        local_logger.info(f"  [9/9] Verifying quorum loss state...")
        try:
            # Try to connect to remaining node
            test_cmd = f'kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach sql --insecure -e "SELECT 1;" 2>&1'
            test_output = self.kubectl.exec_command(test_cmd)
            if "error" in test_output.lower() or "connection" in test_output.lower():
                local_logger.info(f"    ✓ Cluster unresponsive (quorum lost)")
            else:
                local_logger.info(f"    ℹ Cluster may still be responsive")
        except Exception as e:
            local_logger.info(f"    ✓ Cluster unresponsive (quorum lost)")

        local_logger.info(f"\n[Quorum Loss Recovery Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Recover from quorum loss")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Detect quorum loss condition")
        local_logger.info(f"  2. Use unsafe recovery on remaining node (node 0)")
        local_logger.info(f"  3. Verify node 0 functional after recovery")
        local_logger.info(f"  4. Recreate and restart nodes 1 and 2")
        local_logger.info(f"  5. Verify all 3 nodes operational\n")

    @mark_fault_injected
    def recover_fault(self):
        """Clean up resources."""
        pass
