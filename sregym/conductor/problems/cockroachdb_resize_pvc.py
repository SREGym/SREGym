"""
CockroachDB ResizePVC Benchmark - Simulating operator's ResizePVC reconcile action.

This benchmark tests an agent's ability to act as a Kubernetes operator's ResizePVC reconciler.
The agent must expand PersistentVolumeClaims for a CockroachDB cluster from 10Gi to 20Gi by:
1. Patching each PVC with the new storage size
2. Restarting pods to complete filesystem resize
3. Updating StatefulSet VolumeClaimTemplate
4. Maintaining cluster availability throughout

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/resize-pvc/
"""

import logging

from sregym.conductor.oracles.cockroachdb_resize_pvc_oracle import CockroachDBResizePVCOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_RESIZE_PVC_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBResizePVCApp:
    """
    Lightweight app class for CockroachDB ResizePVC benchmark.

    This app does NOT deploy any resources in deploy() - the preconditions
    (3-node initialized cluster with 10Gi PVCs) are set up in inject_fault() instead,
    following the incident management pattern.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-resize-pvc-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB ResizePVC Benchmark - operator ResizePVC action simulation"

    def deploy(self):
        """
        No-op deployment.

        Preconditions (3-node cluster with 10Gi PVCs) are set up in inject_fault() instead,
        which is called by conductor after NOOP stage and before agent starts working.
        """
        local_logger.info(f"[ResizePVC Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent expands PVCs."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[ResizePVC Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBResizePVC(Problem):
    """
    CockroachDB ResizePVC benchmark problem.

    Simulates the operator's ResizePVC action which expands PersistentVolumeClaims
    for a CockroachDB cluster to increase storage capacity.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up 3-node initialized cluster with 10Gi PVCs and updates CR to 20Gi
    4. Agent detects storage mismatch (CR wants 20Gi, PVCs have 10Gi)
    5. Agent patches each PVC, restarts pods, updates StatefulSet template
    6. Oracle validates all PVCs are 20Gi and cluster is healthy
    """

    def __init__(self):
        self.app = CockroachDBResizePVCApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBResizePVCOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Act as CockroachDB operator's ResizePVC reconciler. Expand PVC storage from 10Gi to 20Gi.

OBSERVED STATE:
- CrdbCluster CR spec.dataStore.pvc.resources.requests.storage = 20Gi
- Current PVCs have smaller storage size (10Gi)
- CR has annotation: crdb.cockroachlabs.com/resize-pvc=20Gi
- 3 pods are running with old PVC size (10Gi each)
- Cluster is initialized and healthy

DESIRED STATE:
- All 3 PVCs expanded to 20Gi storage
- Pods restarted to recognize new storage
- Filesystem expanded to use new capacity
- StatefulSet VolumeClaimTemplate updated to 20Gi
- No data loss during expansion
- Cluster remains healthy and functional

YOUR TASK:
Expand persistent volume storage for the CockroachDB cluster from 10Gi to 20Gi.

CONSTRAINTS:
- Maintain cluster quorum throughout (need N/2+1 nodes minimum)
- No data loss during expansion
- Cluster must remain functional after resize

IMPORTANT CONCEPTS:
- Kubernetes supports online PVC expansion if StorageClass allows it
- Pods may need restart to complete filesystem resize
- StatefulSet VolumeClaimTemplate should be updated for future pods

Read the CrdbCluster CR 'crdb-cluster' in namespace 'cockroachdb' for storage configuration.
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
        - StatefulSet with 3 replicas and 10Gi PVCs
        - Initializes the cluster
        - Creates CrdbCluster CR requesting 20Gi storage

        The "fault" here is the mismatch: CR wants 20Gi, but PVCs are 10Gi.
        Agent must expand PVCs to match CR's desired state.
        """
        local_logger.info(f"\n[ResizePVC Benchmark] Setting up preconditions...")

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

        # 3. Create StorageClass with volume expansion enabled
        local_logger.info(f"  [3/9] Creating StorageClass with volume expansion enabled...")
        sc_path = f"{COCKROACH_DB_RESIZE_PVC_RESOURCES}/storageclass.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {sc_path}")
        local_logger.info(f"    ✓ StorageClass created (allowVolumeExpansion=true)")

        # 4. Create RBAC resources
        local_logger.info(f"  [4/9] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_RESIZE_PVC_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 5. Create Services
        local_logger.info(f"  [5/9] Creating Services (Discovery + Public)...")
        services_path = f"{COCKROACH_DB_RESIZE_PVC_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 6. Create StatefulSet with 3 replicas and 10Gi PVCs
        local_logger.info(f"  [6/9] Creating StatefulSet with 3 replicas (10Gi PVCs)...")
        sts_path = f"{COCKROACH_DB_RESIZE_PVC_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    ✓ StatefulSet created")

        # Wait for first pod to be running
        local_logger.info(f"  [6.5/9] Waiting for first pod to be running (for initialization)...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=jsonpath='{{.status.phase}}'=Running pod/{self.cluster_name}-0 --timeout=300s"
            )
            local_logger.info(f"    ✓ Pod {self.cluster_name}-0 is running")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Pod may not be running yet: {e}")

        # Give CockroachDB a moment to start listening
        import time

        local_logger.info(f"  [6.6/9] Waiting 30 seconds for CockroachDB process to start...")
        time.sleep(30)
        local_logger.info(f"    ✓ Wait complete")

        # 7. Initialize the cluster
        local_logger.info(f"  [7/9] Initializing the 3-node cluster...")
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
        local_logger.info(f"  [7.5/9] Waiting for all 3 pods to be ready (now that cluster is initialized)...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod -l app.kubernetes.io/name=cockroachdb --timeout=600s"
            )
            local_logger.info(f"    ✓ All 3 pods are ready")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Not all pods ready yet: {e}")

        # Verify cluster has 3 nodes
        local_logger.info(f"  [7.6/9] Verifying cluster has 3 healthy nodes...")
        try:
            node_status_cmd = (
                f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach node status --insecure"
            )
            result = self.kubectl.exec_command(node_status_cmd)
            local_logger.info(f"    ✓ Cluster has 3 nodes")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Could not verify node status: {e}")

        # Check current PVC sizes (should be 10Gi)
        local_logger.info(f"  [7.7/9] Checking current PVC sizes (should be 10Gi)...")
        try:
            pvc_cmd = f"kubectl -n {self.namespace} get pvc"
            result = self.kubectl.exec_command(pvc_cmd)
            local_logger.info(f"    ✓ Current PVCs:")
            for line in result.split("\n")[:5]:
                local_logger.info(f"      {line}")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Could not check PVC sizes: {e}")

        # 8. Create CrdbCluster CR requesting 20Gi storage
        local_logger.info(f"  [8/9] Creating CrdbCluster CR requesting 20Gi storage...")
        cr_path = f"{COCKROACH_DB_RESIZE_PVC_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created (spec.storage=20Gi)")

        # 8. Verify the mismatch (fault condition)
        local_logger.info(f"\n[ResizePVC Benchmark] Verifying preconditions...")
        try:
            # Check CR wants 20Gi
            cr_cmd = f"kubectl -n {self.namespace} get crdbcluster {self.cluster_name} -o jsonpath='{{.spec.dataStore.pvc.spec.resources.requests.storage}}'"
            cr_storage = self.kubectl.exec_command(cr_cmd)
            local_logger.info(f"  ✓ CrdbCluster CR spec.storage = {cr_storage}")

            # Check PVCs have 10Gi
            pvc_cmd = f"kubectl -n {self.namespace} get pvc datadir-{self.cluster_name}-0 -o jsonpath='{{.spec.resources.requests.storage}}'"
            pvc_storage = self.kubectl.exec_command(pvc_cmd)
            local_logger.info(f"  ✓ PVC current storage = {pvc_storage}")

            if cr_storage.strip() != pvc_storage.strip():
                local_logger.info(
                    f"  ✓ Mismatch detected (fault injected): CR wants {cr_storage}, PVCs have {pvc_storage}"
                )
            else:
                local_logger.info(f"  ⚠️  Warning: No mismatch detected")

        except Exception as e:
            local_logger.info(f"  ⚠️  Warning: Could not verify fault condition: {e}")

        local_logger.info(f"\n[ResizePVC Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Expand PVC storage from 10Gi to 20Gi")
        local_logger.info(f"Expected actions:")
        local_logger.info(
            f'  1. Patch each PVC to 20Gi: kubectl -n {self.namespace} patch pvc datadir-{self.cluster_name}-X -p \'{{"spec":{{"resources":{{"requests":{{"storage":"20Gi"}}}}}}}}\''
        )
        local_logger.info(f"  2. Wait for PVC expansion to complete")
        local_logger.info(
            f"  3. Restart pods to complete filesystem resize: kubectl -n {self.namespace} delete pod {self.cluster_name}-X"
        )
        local_logger.info(
            f'  4. Update StatefulSet VolumeClaimTemplate: kubectl -n {self.namespace} patch sts {self.cluster_name} --type=json -p=\'[{{"op":"replace","path":"/spec/volumeClaimTemplates/0/spec/resources/requests/storage","value":"20Gi"}}]\''
        )
        local_logger.info(f"  5. Verify all PVCs are 20Gi and cluster is healthy\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup() which is called by conductor
        pass
