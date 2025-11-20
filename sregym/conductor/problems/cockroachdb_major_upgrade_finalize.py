"""
CockroachDB Major Upgrade Finalize Benchmark - Finalizing a major version upgrade.

This benchmark tests an agent's ability to finalize a major version upgrade
by resetting the preserve_downgrade_option cluster setting.

The agent must:
1. Verify cluster is running new version (v24.1.0)
2. Check preserve_downgrade_option is set to old version
3. Reset preserve_downgrade_option to enable finalization
4. Verify migrations are complete
5. Ensure cluster cannot downgrade to old version anymore

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/major-upgrade-finalize/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_major_upgrade_finalize_oracle import CockroachDBMajorUpgradeFinalizeOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_MAJOR_UPGRADE_FINALIZE_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBMajorUpgradeFinalizeApp:
    """Lightweight app class for CockroachDB Major Upgrade Finalize benchmark."""

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-major-upgrade-finalize-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Major Upgrade Finalize Benchmark"

    def deploy(self):
        """No-op deployment."""
        local_logger.info(f"[Major Upgrade Finalize Benchmark] App deploy() called - no action needed")

    def start_workload(self):
        """No workload to start."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Major Upgrade Finalize Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBMajorUpgradeFinalize(Problem):
    """
    CockroachDB Major Upgrade Finalize benchmark problem.

    Simulates finalizing a major version upgrade.
    """

    def __init__(self):
        self.app = CockroachDBMajorUpgradeFinalizeApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"
        self.old_version = "23.2"
        self.new_version = "24.1.0"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBMajorUpgradeFinalizeOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Finalize major version upgrade by resetting preserve_downgrade_option.

OBSERVED STATE:
- Cluster running v24.1.0 (new version)
- All pods have completed version upgrade to v24.1.0
- preserve_downgrade_option='23.2' (allows downgrade)
- CR has annotation: crdb.cockroachlabs.com/upgrade-finalize=v24.1.0
- Cluster is healthy but upgrade not yet finalized

DESIRED STATE:
- preserve_downgrade_option reset (undefined/empty)
- Major version finalization migration complete
- Cluster locked to new version (no downgrade possible)
- All migration scripts executed
- Cluster remains healthy and responsive

YOUR TASK:
Finalize the major version upgrade by resetting preserve_downgrade_option.

CONSTRAINTS:
- Cannot downgrade after finalization (this is permanent)
- Finalization must complete before production workload
- Verify no pending migrations before finalizing
- Ensure cluster remains available during finalization

IMPORTANT CONCEPTS:
- preserve_downgrade_option allows testing before finalization
- Resetting it triggers final migration scripts
- Once reset, cluster cannot downgrade to old version
- Finalization is irreversible

Reset the preserve_downgrade_option cluster setting to finalize the upgrade."""

    @mark_fault_injected
    def inject_fault(self):
        """
        Set up preconditions for the benchmark.

        Creates a cluster that has been upgraded to v24.1.0 but still has
        preserve_downgrade_option set to allow downgrade testing.
        """
        local_logger.info(f"\n[Major Upgrade Finalize Benchmark] Setting up preconditions...")

        # 1. Create namespace
        local_logger.info(f"  [1/8] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            local_logger.info(f"    ✓ Namespace created")
        except Exception as e:
            local_logger.info(f"    ℹ Namespace may already exist: {e}")

        # 2. Install CockroachDB CRDs
        local_logger.info(f"  [2/8] Installing CockroachDB CRDs...")
        crd_url = "https://raw.githubusercontent.com/cockroachdb/cockroach-operator/master/install/crds.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {crd_url}")
        local_logger.info(f"    ✓ CRDs installed")

        # 2.5. Delete validating webhook
        local_logger.info(f"  [2.5/8] Removing validating webhook...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    ✓ Webhook removed")
        except Exception as e:
            local_logger.info(f"    ℹ Webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/8] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_MAJOR_UPGRADE_FINALIZE_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/8] Creating Services...")
        services_path = f"{COCKROACH_DB_MAJOR_UPGRADE_FINALIZE_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 5. Create StatefulSet with v24.1.0 (already upgraded)
        local_logger.info(f"  [5/8] Creating StatefulSet with v24.1.0...")
        sts_path = f"{COCKROACH_DB_MAJOR_UPGRADE_FINALIZE_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    ✓ StatefulSet created")

        # Wait for first pod
        local_logger.info(f"  [5.5/8] Waiting for pods to be running...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=jsonpath='{{.status.phase}}'=Running pod/{self.cluster_name}-0 --timeout=300s"
            )
            local_logger.info(f"    ✓ Pod running")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Pod may not be running yet: {e}")

        time.sleep(30)

        # 6. Initialize cluster
        local_logger.info(f"  [6/8] Initializing cluster...")
        try:
            init_cmd = f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach init --insecure"
            result = self.kubectl.exec_command(init_cmd)
            local_logger.info(f"    ✓ Cluster initialized")
        except Exception as e:
            if "already initialized" not in str(e).lower():
                local_logger.info(f"    ℹ Cluster may already be initialized: {e}")

        # Wait for all pods
        local_logger.info(f"  [6.5/8] Waiting for all pods to be ready...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod -l app.kubernetes.io/name=cockroachdb --timeout=600s"
            )
            local_logger.info(f"    ✓ All pods ready")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Not all pods ready: {e}")

        # 7. Set preserve_downgrade_option for testing phase
        local_logger.info(f"  [7/8] Setting preserve_downgrade_option to '{self.old_version}'...")
        try:
            sql_cmd = f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach sql --insecure -e \"SET CLUSTER SETTING cluster.preserve_downgrade_option = '{self.old_version}';\""
            result = self.kubectl.exec_command(sql_cmd)
            local_logger.info(f"    ✓ preserve_downgrade_option set to {self.old_version}")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Could not set preserve_downgrade_option: {e}")

        # 8. Create CrdbCluster CR requesting finalization
        local_logger.info(f"  [8/8] Creating CrdbCluster CR...")
        cr_path = f"{COCKROACH_DB_MAJOR_UPGRADE_FINALIZE_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created")

        local_logger.info(f"\n[Major Upgrade Finalize Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Finalize major version upgrade")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Verify cluster is running v24.1.0")
        local_logger.info(f"  2. Check preserve_downgrade_option is set to {self.old_version}")
        local_logger.info(f"  3. Reset preserve_downgrade_option to finalize")
        local_logger.info(f"  4. Verify migrations complete")
        local_logger.info(f"  5. Confirm cluster locked to new version\n")

    @mark_fault_injected
    def recover_fault(self):
        """Clean up resources."""
        pass
