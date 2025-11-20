"""
CockroachDB Cluster Settings Benchmark - Configuring cluster-wide settings via SQL.

This benchmark tests an agent's ability to apply cluster settings across a
CockroachDB cluster using SQL commands and verify the configuration is persisted.

The agent must:
1. Connect to the cluster via SQL
2. Apply required cluster settings (SET CLUSTER SETTING)
3. Verify settings are applied correctly
4. Ensure settings persist across pod restarts
5. Remove the annotation when complete

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/cluster-settings/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_cluster_settings_oracle import CockroachDBClusterSettingsOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_CLUSTER_SETTINGS_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBClusterSettingsApp:
    """
    Lightweight app class for CockroachDB Cluster Settings benchmark.

    This app does NOT deploy resources in deploy() - the preconditions
    are set up in inject_fault() instead.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-cluster-settings-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Cluster Settings Benchmark - configuring cluster settings via SQL"

    def deploy(self):
        """
        No-op deployment.

        Preconditions (3-node cluster) are set up in inject_fault().
        """
        local_logger.info(f"[Cluster Settings Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent configures cluster settings."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Cluster Settings Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  Cleanup complete")


class CockroachDBClusterSettings(Problem):
    """
    CockroachDB Cluster Settings benchmark problem.

    Simulates the operator's ClusterSettings action which applies cluster-wide
    configuration settings via SQL commands.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up 3-node cluster with annotation
    4. Agent detects annotation: crdb.cockroachlabs.com/cluster-settings=update-required
    5. Agent applies cluster settings via SQL (e.g., SET CLUSTER SETTING sql.defaults.timezone='UTC')
    6. Agent verifies settings are applied
    7. Agent removes annotation
    8. Oracle validates settings persisted and annotation removed
    """

    def __init__(self):
        self.app = CockroachDBClusterSettingsApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBClusterSettingsOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Act as CockroachDB operator's ClusterSettings reconciler. Apply cluster settings via SQL.

OBSERVED STATE:
- 3-node CockroachDB cluster is running and healthy
- CrdbCluster CR has annotation: crdb.cockroachlabs.com/cluster-settings=update-required
- Cluster settings not yet configured

DESIRED STATE:
- Cluster settings applied via SQL (e.g., timezone, SQL defaults)
- Settings persisted and active in the cluster
- Settings verified readable from system.settings table
- Annotation removed from CrdbCluster CR
- Cluster remains healthy

YOUR TASK:
Apply cluster settings to the CockroachDB cluster via SQL commands.

CONSTRAINTS:
- Use SQL commands to configure settings (SET CLUSTER SETTING)
- Settings must be queryable from system.settings
- Cluster must remain available during configuration
- Remove annotation when complete

IMPORTANT CONCEPTS:
- CockroachDB cluster settings control behavior across the entire cluster
- Settings are applied via SET CLUSTER SETTING <setting>=<value> SQL commands
- Settings are stored in the system.settings table
- Different settings have different value types (string, integer, duration, etc.)

Read the CrdbCluster CR 'crdb-cluster' in namespace 'cockroachdb' to understand
the configuration requirements from the annotation. Investigate cluster state and
apply the necessary settings."""

    @mark_fault_injected
    def inject_fault(self):
        """
        Set up preconditions for the benchmark.

        Creates:
        - Namespace
        - CockroachDB CRDs
        - RBAC resources
        - Services
        - StatefulSet with 3 replicas
        - Initializes the cluster
        - Creates CrdbCluster CR with cluster-settings annotation

        The "fault" here is the missing cluster settings configuration.
        """
        local_logger.info(f"\n[Cluster Settings Benchmark] Setting up preconditions...")

        # 1. Create namespace
        local_logger.info(f"  [1/9] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            if "AlreadyExists" in result:
                local_logger.info(f"    Namespace already exists")
            else:
                local_logger.info(f"    Namespace created")
        except Exception as e:
            local_logger.info(f"    Namespace may already exist: {e}")

        # 2. Install CockroachDB CRDs
        local_logger.info(f"  [2/9] Installing CockroachDB CRDs...")
        crd_url = "https://raw.githubusercontent.com/cockroachdb/cockroach-operator/master/install/crds.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {crd_url}")
        local_logger.info(f"    CRDs installed")

        # 2.5. Delete validating webhook
        local_logger.info(f"  [2.5/9] Removing validating webhook (no operator in this benchmark)...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    Webhook removed")
        except Exception as e:
            local_logger.info(f"    Webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/9] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_CLUSTER_SETTINGS_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/9] Creating Services...")
        services_path = f"{COCKROACH_DB_CLUSTER_SETTINGS_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    Services created")

        # 5. Create StatefulSet with 3 replicas
        local_logger.info(f"  [5/9] Creating StatefulSet with 3 replicas...")
        sts_path = f"{COCKROACH_DB_CLUSTER_SETTINGS_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    StatefulSet created")

        # Wait for first pod to be running
        local_logger.info(f"  [5.5/9] Waiting for first pod to be running...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=jsonpath='{{.status.phase}}'=Running pod/{self.cluster_name}-0 --timeout=300s"
            )
            local_logger.info(f"    Pod is running")
        except Exception as e:
            local_logger.info(f"    Warning: Pod may not be running yet: {e}")

        # Give CockroachDB a moment to start listening
        local_logger.info(f"  [5.6/9] Waiting 30 seconds for CockroachDB process to start...")
        time.sleep(30)
        local_logger.info(f"    Wait complete")

        # 6. Initialize the cluster
        local_logger.info(f"  [6/9] Initializing the 3-node cluster...")
        max_retries = 5
        for attempt in range(max_retries):
            try:
                init_cmd = f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach init --insecure"
                result = self.kubectl.exec_command(init_cmd)
                local_logger.info(f"    Cluster initialized")
                break
            except Exception as e:
                if "already initialized" in str(e).lower():
                    local_logger.info(f"    Cluster already initialized")
                    break
                elif attempt < max_retries - 1:
                    local_logger.info(f"    Init attempt {attempt + 1} failed, retrying in 10s")
                    time.sleep(10)
                else:
                    local_logger.info(f"    Warning: Could not initialize cluster: {e}")

        # Wait for all 3 pods to be ready
        local_logger.info(f"  [6.5/9] Waiting for all 3 pods to be ready...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod -l app.kubernetes.io/name=cockroachdb --timeout=600s"
            )
            local_logger.info(f"    All 3 pods are ready")
        except Exception as e:
            local_logger.info(f"    Warning: Not all pods ready yet: {e}")

        # Verify cluster has 3 nodes
        local_logger.info(f"  [7/9] Verifying cluster has 3 healthy nodes...")
        try:
            node_status_cmd = (
                f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach node status --insecure"
            )
            result = self.kubectl.exec_command(node_status_cmd)
            local_logger.info(f"    Cluster has 3 nodes")
        except Exception as e:
            local_logger.info(f"    Warning: Could not verify node status: {e}")

        # 8. Create CrdbCluster CR with cluster-settings annotation
        local_logger.info(f"  [8/9] Creating CrdbCluster CR with cluster-settings annotation...")
        cr_path = f"{COCKROACH_DB_CLUSTER_SETTINGS_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    CrdbCluster CR created with cluster-settings annotation")

        # 9. Verify preconditions
        local_logger.info(f"\n[Cluster Settings Benchmark] Verifying preconditions...")
        try:
            # Check annotation exists
            annotation_cmd = f"kubectl -n {self.namespace} get crdbcluster {self.cluster_name} -o jsonpath='{{.metadata.annotations.crdb\\.cockroachlabs\\.com/cluster-settings}}'"
            annotation = self.kubectl.exec_command(annotation_cmd)
            local_logger.info(f"  Annotation value: {annotation}")
            if annotation == "update-required":
                local_logger.info(f"  Annotation detected (fault injected)")
            else:
                local_logger.info(f"  Warning: Annotation may not be correct")
        except Exception as e:
            local_logger.info(f"  Warning: Could not verify annotation: {e}")

        local_logger.info(f"\n[Cluster Settings Benchmark] Preconditions complete!")
        local_logger.info(f"\n Agent task: Apply cluster settings via SQL")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Connect to cluster via SQL")
        local_logger.info(f"  2. Apply cluster settings (SET CLUSTER SETTING)")
        local_logger.info(f"  3. Verify settings are applied")
        local_logger.info(f"  4. Confirm settings persist in system.settings")
        local_logger.info(f"  5. Remove the cluster-settings annotation\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup()
        pass
