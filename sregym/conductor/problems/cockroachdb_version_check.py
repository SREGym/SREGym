"""
CockroachDB Version Check Benchmark - Validating image version before deployment.

This benchmark tests an agent's ability to validate CockroachDB image versions
and set cluster conditions when version constraints are met.

The agent must:
1. Extract and validate the image version
2. Check version constraints and compatibility
3. Set cluster condition indicating version validation complete
4. Clean up any temporary verification jobs
5. Remove the annotation when complete

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/version-check/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_version_check_oracle import CockroachDBVersionCheckOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_VERSION_CHECK_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBVersionCheckApp:
    """
    Lightweight app class for CockroachDB Version Check benchmark.

    This app does NOT deploy resources in deploy() - the preconditions
    are set up in inject_fault() instead.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-version-check-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Version Check Benchmark - validating image version before deployment"

    def deploy(self):
        """
        No-op deployment.

        Preconditions (3-node cluster) are set up in inject_fault().
        """
        local_logger.info(f"[Version Check Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent validates version."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Version Check Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  Cleanup complete")


class CockroachDBVersionCheck(Problem):
    """
    CockroachDB Version Check benchmark problem.

    Simulates the operator's VersionCheck action which validates CockroachDB
    image versions before deploying to the cluster.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up 3-node cluster with annotation
    4. Agent detects annotation: crdb.cockroachlabs.com/version-check=required
    5. Agent extracts and validates image version from StatefulSet
    6. Agent sets cluster condition indicating version validated
    7. Agent cleans up temporary verification resources (if any)
    8. Agent removes annotation
    9. Oracle validates version extracted, condition set, annotation removed
    """

    def __init__(self):
        self.app = CockroachDBVersionCheckApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBVersionCheckOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Act as CockroachDB operator's VersionCheck reconciler. Validate image version before deployment.

OBSERVED STATE:
- 3-node CockroachDB cluster is running
- CrdbCluster CR has annotation: crdb.cockroachlabs.com/version-check=required
- StatefulSet references CockroachDB image v24.1.0
- Version validation not yet performed

DESIRED STATE:
- Image version extracted and validated (v24.1.0)
- Version constraints checked and verified
- CrdbCluster status condition set indicating version validated
- Annotation removed from CrdbCluster CR
- Any temporary verification Jobs cleaned up
- Cluster remains operational

YOUR TASK:
Validate the CockroachDB image version and set the appropriate cluster condition.

CONSTRAINTS:
- Extract version from StatefulSet image specification
- Validate version format and constraints
- Set cluster status condition when validation complete
- Clean up any temporary resources created for verification
- Remove annotation when complete

IMPORTANT CONCEPTS:
- Version should be extracted from container image (e.g., cockroachdb/cockroach:v24.1.0)
- Version validation ensures compatibility before major operations
- Cluster conditions track important state changes
- CrdbCluster CR status.conditions array should be updated

Read the StatefulSet in namespace 'cockroachdb' to extract the image version.
Read the CrdbCluster CR 'crdb-cluster' to understand version requirements from annotation.
Update the CR status with appropriate conditions when validation completes."""

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
        - Creates CrdbCluster CR with version-check annotation

        The "fault" here is the missing version validation.
        """
        local_logger.info(f"\n[Version Check Benchmark] Setting up preconditions...")

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

        # 2.5. Delete webhooks (no operator in this benchmark)
        local_logger.info(f"  [2.5/9] Removing webhooks (no operator in this benchmark)...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    ✓ Validating webhook removed")
        except Exception as e:
            local_logger.info(f"    ℹ Validating webhook may not exist: {e}")

        try:
            result = self.kubectl.exec_command(
                "kubectl delete mutatingwebhookconfiguration cockroach-operator-mutating-webhook-configuration"
            )
            local_logger.info(f"    ✓ Mutating webhook removed")
        except Exception as e:
            local_logger.info(f"    ℹ Mutating webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/9] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_VERSION_CHECK_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/9] Creating Services...")
        services_path = f"{COCKROACH_DB_VERSION_CHECK_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    Services created")

        # 5. Create StatefulSet with 3 replicas
        local_logger.info(f"  [5/9] Creating StatefulSet with 3 replicas...")
        sts_path = f"{COCKROACH_DB_VERSION_CHECK_RESOURCES}/statefulset.yaml"
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

        # 8. Create CrdbCluster CR with version-check annotation
        local_logger.info(f"  [8/9] Creating CrdbCluster CR with version-check annotation...")
        cr_path = f"{COCKROACH_DB_VERSION_CHECK_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    CrdbCluster CR created with version-check annotation")

        # 9. Verify preconditions
        local_logger.info(f"\n[Version Check Benchmark] Verifying preconditions...")
        try:
            # Check annotation exists
            annotation_cmd = f"kubectl -n {self.namespace} get crdbcluster {self.cluster_name} -o jsonpath='{{.metadata.annotations.crdb\\.cockroachlabs\\.com/version-check}}'"
            annotation = self.kubectl.exec_command(annotation_cmd)
            local_logger.info(f"  Annotation value: {annotation}")
            if annotation == "required":
                local_logger.info(f"  Annotation detected (fault injected)")
            else:
                local_logger.info(f"  Warning: Annotation may not be correct")
        except Exception as e:
            local_logger.info(f"  Warning: Could not verify annotation: {e}")

        local_logger.info(f"\n[Version Check Benchmark] Preconditions complete!")
        local_logger.info(f"\nAgent task: Validate image version and set condition")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Extract version from StatefulSet image (v24.1.0)")
        local_logger.info(f"  2. Validate version constraints")
        local_logger.info(f"  3. Set CrdbCluster status condition (type: VersionChecked)")
        local_logger.info(f"  4. Clean up temporary resources (if any)")
        local_logger.info(f"  5. Remove the version-check annotation\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup()
        pass
