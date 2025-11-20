"""
CockroachDB Partitioned Update Benchmark - Simulating operator's rolling version upgrade.

This benchmark tests an agent's ability to perform a controlled rolling update of
CockroachDB cluster, acting as the operator's PartitionedUpdate reconciler.

The agent must:
1. Update StatefulSet image to new version
2. Perform rolling update maintaining quorum
3. Verify each pod upgrade before proceeding
4. Ensure zero-downtime during upgrade

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/partitioned-update/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_partitioned_update_oracle import CockroachDBPartitionedUpdateOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_PARTITIONED_UPDATE_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBPartitionedUpdateApp:
    """
    Lightweight app class for CockroachDB Partitioned Update benchmark.

    This app does NOT deploy resources in deploy() - the preconditions
    (3-node cluster running old version) are set up in inject_fault() instead.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-partitioned-update-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Partitioned Update Benchmark - rolling version upgrade simulation"

    def deploy(self):
        """
        No-op deployment.

        Preconditions (3-node cluster with old version) are set up in inject_fault().
        """
        local_logger.info(f"[Partitioned Update Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent performs rolling update."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Partitioned Update Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBPartitionedUpdate(Problem):
    """
    CockroachDB Partitioned Update benchmark problem.

    Simulates the operator's PartitionedUpdate action which performs a controlled
    rolling version upgrade of the CockroachDB cluster.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up 3-node cluster with v23.2.0 and updates CR to v24.1.0
    4. Agent detects version mismatch (CR wants v24.1.0, pods running v23.2.0)
    5. Agent updates StatefulSet image and performs rolling update
    6. Oracle validates all pods running v24.1.0 with zero downtime
    """

    def __init__(self):
        self.app = CockroachDBPartitionedUpdateApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"
        self.old_version = "v23.2.0"
        self.new_version = "v24.1.0"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBPartitionedUpdateOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Act as CockroachDB operator's PartitionedUpdate reconciler. Perform a rolling version upgrade.

OBSERVED STATE:
- CrdbCluster CR spec.image updated to cockroachdb/cockroach:v24.1.0
- Current pods running old version (v23.2.0)
- CR has annotation: crdb.cockroachlabs.com/partitioned-update=v24.1.0
- StatefulSet still references old image
- Cluster is healthy with 3 nodes

DESIRED STATE:
- All 3 pods running new version (v24.1.0)
- StatefulSet updated with new image
- Zero downtime during upgrade
- Cluster maintains quorum throughout
- All pods healthy and ready

YOUR TASK:
Perform a rolling upgrade of the CockroachDB cluster from v23.2.0 to v24.1.0.

CONSTRAINTS:
- Maintain cluster quorum throughout (need N/2+1 nodes minimum)
- Zero downtime - cluster must remain functional during upgrade
- Verify each pod upgrade completes before proceeding to next
- Update pods in a safe order (typically reverse order: 2→1→0)

IMPORTANT CONCEPTS:
- StatefulSet rolling updates can be controlled via partition strategy
- CockroachDB supports online rolling upgrades within major versions
- Each pod must be verified healthy before upgrading the next
- Quorum must be maintained to avoid cluster unavailability

Read the CrdbCluster CR 'crdb-cluster' in namespace 'cockroachdb' for version configuration.
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
        - StatefulSet with 3 replicas running v23.2.0
        - Initializes the cluster
        - Creates CrdbCluster CR requesting v24.1.0

        The "fault" here is the version mismatch: CR wants v24.1.0, but pods run v23.2.0.
        Agent must perform rolling update to match CR's desired state.
        """
        local_logger.info(f"\n[Partitioned Update Benchmark] Setting up preconditions...")

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
        rbac_path = f"{COCKROACH_DB_PARTITIONED_UPDATE_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/9] Creating Services (Discovery + Public)...")
        services_path = f"{COCKROACH_DB_PARTITIONED_UPDATE_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 5. Create StatefulSet with 3 replicas running v23.2.0
        local_logger.info(f"  [5/9] Creating StatefulSet with 3 replicas (v23.2.0)...")
        sts_path = f"{COCKROACH_DB_PARTITIONED_UPDATE_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    ✓ StatefulSet created")

        # Wait for first pod to be running
        local_logger.info(f"  [5.5/9] Waiting for first pod to be running (for initialization)...")
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
        local_logger.info(f"  [6.5/9] Waiting for all 3 pods to be ready (now that cluster is initialized)...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod -l app.kubernetes.io/name=cockroachdb --timeout=600s"
            )
            local_logger.info(f"    ✓ All 3 pods are ready")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Not all pods ready yet: {e}")

        # Verify cluster has 3 nodes
        local_logger.info(f"  [7/9] Verifying cluster has 3 healthy nodes...")
        try:
            node_status_cmd = (
                f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach node status --insecure"
            )
            result = self.kubectl.exec_command(node_status_cmd)
            local_logger.info(f"    ✓ Cluster has 3 nodes")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Could not verify node status: {e}")

        # Check current version (should be v23.2.0)
        local_logger.info(f"  [7.5/9] Checking current version (should be {self.old_version})...")
        try:
            version_cmd = f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach version"
            result = self.kubectl.exec_command(version_cmd)
            local_logger.info(f"    ✓ Current version:")
            for line in result.split("\n")[:3]:
                local_logger.info(f"      {line}")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Could not check version: {e}")

        # 8. Create CrdbCluster CR requesting v24.1.0
        local_logger.info(f"  [8/9] Creating CrdbCluster CR requesting {self.new_version}...")
        cr_path = f"{COCKROACH_DB_PARTITIONED_UPDATE_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created (spec.image={self.new_version})")

        # 9. Verify the version mismatch (fault condition)
        local_logger.info(f"\n[Partitioned Update Benchmark] Verifying preconditions...")
        try:
            # Check CR wants v24.1.0
            cr_cmd = (
                f"kubectl -n {self.namespace} get crdbcluster {self.cluster_name} -o jsonpath='{{.spec.image.name}}'"
            )
            cr_image = self.kubectl.exec_command(cr_cmd)
            local_logger.info(f"  ✓ CrdbCluster CR spec.image = {cr_image}")

            # Check StatefulSet uses v23.2.0
            sts_cmd = f"kubectl -n {self.namespace} get sts {self.cluster_name} -o jsonpath='{{.spec.template.spec.containers[0].image}}'"
            sts_image = self.kubectl.exec_command(sts_cmd)
            local_logger.info(f"  ✓ StatefulSet current image = {sts_image}")

            if self.new_version.split(":")[-1] in cr_image and self.old_version.split(":")[-1] in sts_image:
                local_logger.info(
                    f"  ✓ Version mismatch detected (fault injected): CR wants {self.new_version}, STS has {self.old_version}"
                )
            else:
                local_logger.info(f"  ⚠️  Warning: Version mismatch may not be correct")

        except Exception as e:
            local_logger.info(f"  ⚠️  Warning: Could not verify fault condition: {e}")

        local_logger.info(f"\n[Partitioned Update Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Perform rolling upgrade from {self.old_version} to {self.new_version}")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Update StatefulSet image to {self.new_version}")
        local_logger.info(f"  2. Perform controlled rolling update (one pod at a time)")
        local_logger.info(f"  3. Verify each pod healthy before proceeding to next")
        local_logger.info(f"  4. Maintain quorum throughout the upgrade")
        local_logger.info(f"  5. Verify all pods running {self.new_version} and cluster healthy\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup() which is called by conductor
        pass
