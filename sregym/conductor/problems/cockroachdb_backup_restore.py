"""
CockroachDB Backup & Restore Benchmark - Configuring and testing backup functionality.

This benchmark tests an agent's ability to configure backup using nodelocal storage
and verify backup/restore capabilities.

The agent must:
1. Configure backup destination (nodelocal storage)
2. Create a backup of the cluster data
3. Verify backup metadata
4. Restore from backup to ensure data integrity
5. Verify cluster data after restoration

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/backup-restore/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_backup_restore_oracle import CockroachDBBackupRestoreOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_BACKUP_RESTORE_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBBackupRestoreApp:
    """
    Lightweight app class for CockroachDB Backup & Restore benchmark.

    This app does NOT deploy resources in deploy() - the preconditions
    are set up in inject_fault() instead.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-backup-restore-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Backup & Restore Benchmark"

    def deploy(self):
        """
        No-op deployment.

        Preconditions are set up in inject_fault().
        """
        local_logger.info(f"[Backup Restore Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent performs backup configuration."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Backup Restore Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBBackupRestore(Problem):
    """
    CockroachDB Backup & Restore benchmark problem.

    Simulates the operator's backup configuration and testing workflow.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up 3-node cluster with sample data
    4. Agent configures backup using nodelocal storage
    5. Agent creates backup and verifies integrity
    6. Oracle validates backup was created, metadata exists, and restore is possible
    """

    def __init__(self):
        self.app = CockroachDBBackupRestoreApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBBackupRestoreOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Configure CockroachDB backup using nodelocal storage and test restore functionality.

OBSERVED STATE:
- CockroachDB cluster running with 3 nodes (v24.1.0)
- Sample data exists in the cluster
- CR has annotation: crdb.cockroachlabs.com/backup=required
- No backup configured yet
- Backup destination not set up

DESIRED STATE:
- Backup destination configured (nodelocal storage)
- Full cluster backup created and stored
- Backup metadata verified and accessible
- Restore from backup is possible and data verified
- Backup has proper retention and scheduling

YOUR TASK:
Configure backup for the CockroachDB cluster and verify backup/restore workflow.

CONSTRAINTS:
- Use nodelocal storage for backup destination
- Ensure backup captures all data and system tables
- Verify backup metadata is complete
- Test restore capability without actually restoring (unless needed)

IMPORTANT CONCEPTS:
- CockroachDB supports full and incremental backups
- Backups can be stored on various destinations (s3, gcs, nodelocal, etc.)
- Nodelocal storage stores backup files on cluster nodes
- Backup metadata includes schema, timestamps, and restore points

Read the CrdbCluster CR 'crdb-cluster' in namespace 'cockroachdb' for backup configuration.
Investigate current cluster state and determine the necessary steps to achieve the desired state."""

    @mark_fault_injected
    def inject_fault(self):
        """
        Set up preconditions for the benchmark.

        This is called by conductor after NOOP stage, before agent starts working.

        Creates:
        - Namespace
        - CockroachDB CRDs
        - RBAC resources
        - Services
        - StatefulSet with 3 replicas running v24.1.0
        - Initializes the cluster
        - Creates sample data for backup testing
        - Creates CrdbCluster CR requesting backup configuration
        """
        local_logger.info(f"\n[Backup Restore Benchmark] Setting up preconditions...")

        # 1. Create namespace
        local_logger.info(f"  [1/9] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            if "AlreadyExists" in result:
                local_logger.info(f"    ℹ Namespace already exists")
            else:
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
        rbac_path = f"{COCKROACH_DB_BACKUP_RESTORE_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/9] Creating Services...")
        services_path = f"{COCKROACH_DB_BACKUP_RESTORE_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 5. Create StatefulSet with 3 replicas running v24.1.0
        local_logger.info(f"  [5/9] Creating StatefulSet with 3 replicas (v24.1.0)...")
        sts_path = f"{COCKROACH_DB_BACKUP_RESTORE_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    ✓ StatefulSet created")

        # Wait for first pod to be running
        local_logger.info(f"  [5.5/9] Waiting for first pod to be running...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=jsonpath='{{.status.phase}}'=Running pod/{self.cluster_name}-0 --timeout=300s"
            )
            local_logger.info(f"    ✓ Pod {self.cluster_name}-0 is running")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Pod may not be running yet: {e}")

        # Give CockroachDB a moment to start listening
        local_logger.info(f"  [5.6/9] Waiting 30 seconds for CockroachDB process to start...")
        time.sleep(30)
        local_logger.info(f"    ✓ Wait complete")

        # 6. Initialize the cluster
        local_logger.info(f"  [6/9] Initializing the 3-node cluster...")
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

        # Now wait for all 3 pods to be ready
        local_logger.info(f"  [6.5/9] Waiting for all 3 pods to be ready...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod -l app.kubernetes.io/name=cockroachdb --timeout=600s"
            )
            local_logger.info(f"    ✓ All 3 pods are ready")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Not all pods ready yet: {e}")

        # 7. Create sample data for backup testing
        local_logger.info(f"  [7/9] Creating sample data for backup testing...")
        try:
            sql_cmd = f'kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach sql --insecure -e \'CREATE DATABASE IF NOT EXISTS testdb; CREATE TABLE IF NOT EXISTS testdb.test_table (id INT, name STRING); INSERT INTO testdb.test_table VALUES (1, "test1"), (2, "test2");\''
            result = self.kubectl.exec_command(sql_cmd)
            local_logger.info(f"    ✓ Sample data created")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Could not create sample data: {e}")

        # 8. Create CrdbCluster CR requesting backup configuration
        local_logger.info(f"  [8/9] Creating CrdbCluster CR...")
        cr_path = f"{COCKROACH_DB_BACKUP_RESTORE_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created with backup annotation")

        # 9. Verify preconditions
        local_logger.info(f"\n[Backup Restore Benchmark] Verifying preconditions...")
        try:
            cr_cmd = f"kubectl -n {self.namespace} get crdbcluster {self.cluster_name} -o jsonpath='{{.metadata.annotations.crdb\\.cockroachlabs\\.com/backup}}'"
            cr_backup = self.kubectl.exec_command(cr_cmd)
            local_logger.info(f"  ✓ CrdbCluster CR has backup annotation: {cr_backup}")
        except Exception as e:
            local_logger.info(f"  ⚠️  Warning: Could not verify backup annotation: {e}")

        local_logger.info(f"\n[Backup Restore Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Configure backup and test restore functionality")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Configure backup destination (nodelocal storage)")
        local_logger.info(f"  2. Create a full backup of the cluster")
        local_logger.info(f"  3. Verify backup metadata is complete")
        local_logger.info(f"  4. Test restore capability (verify data can be restored)")
        local_logger.info(f"  5. Verify backup has proper retention\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup()
        pass
