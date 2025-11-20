"""
CockroachDB Multi-Region Setup Benchmark - Configuring multi-region topology.

This benchmark tests an agent's ability to configure a CockroachDB cluster
for multi-region deployment with proper locality settings and pod anti-affinity.

The agent must:
1. Configure pod anti-affinity rules
2. Set locality flags on nodes
3. Configure region-aware replication
4. Verify topology configuration
5. Ensure pods are spread across zones

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/multi-region-setup/
"""

import logging

from sregym.conductor.oracles.cockroachdb_multi_region_setup_oracle import CockroachDBMultiRegionSetupOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_MULTI_REGION_SETUP_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBMultiRegionSetupApp:
    """Lightweight app class for CockroachDB Multi-Region Setup benchmark."""

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-multi-region-setup-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Multi-Region Setup Benchmark"

    def deploy(self):
        """No-op deployment."""
        local_logger.info(f"[Multi-Region Setup Benchmark] App deploy() called - no action needed")

    def start_workload(self):
        """No workload to start."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Multi-Region Setup Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBMultiRegionSetup(Problem):
    """CockroachDB Multi-Region Setup benchmark problem."""

    def __init__(self):
        self.app = CockroachDBMultiRegionSetupApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBMultiRegionSetupOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Configure multi-region topology for CockroachDB cluster.

OBSERVED STATE:
- CockroachDB cluster with 3 nodes deployed
- CR has annotation: crdb.cockroachlabs.com/multi-region=required
- No locality settings configured
- No pod anti-affinity rules
- Cluster not optimized for multi-region deployment

DESIRED STATE:
- Pod anti-affinity configured (pods spread across zones)
- Locality flags set on all nodes (region/zone awareness)
- Multi-region replication policy configured
- All pods running on different nodes
- Topology constraints enforced

YOUR TASK:
Configure the CockroachDB cluster for multi-region deployment.

CONSTRAINTS:
- Ensure pods spread across different nodes
- Set locality flags for region/zone awareness
- Configure pod anti-affinity preferences
- Maintain cluster availability during configuration
- Verify topology spread

IMPORTANT CONCEPTS:
- Locality flags identify node location (region, zone)
- Pod anti-affinity prevents pods on same node
- Multi-region setup improves data locality
- Topology-aware placement improves performance

Configure StatefulSet with anti-affinity and set locality flags."""

    @mark_fault_injected
    def inject_fault(self):
        """Set up preconditions for the benchmark."""
        local_logger.info(f"\n[Multi-Region Setup Benchmark] Setting up preconditions...")

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
        rbac_path = f"{COCKROACH_DB_MULTI_REGION_SETUP_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/7] Creating Services...")
        services_path = f"{COCKROACH_DB_MULTI_REGION_SETUP_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 5. Create StatefulSet with multi-region settings
        local_logger.info(f"  [5/7] Creating StatefulSet with multi-region topology...")
        sts_path = f"{COCKROACH_DB_MULTI_REGION_SETUP_RESOURCES}/statefulset.yaml"
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
        cr_path = f"{COCKROACH_DB_MULTI_REGION_SETUP_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created")

        local_logger.info(f"\n[Multi-Region Setup Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Configure multi-region topology\n")

    @mark_fault_injected
    def recover_fault(self):
        """Clean up resources."""
        pass
