"""
CockroachDB Deploy Benchmark - Simulating operator's Deploy reconcile action.

This benchmark tests an agent's ability to act as a Kubernetes operator's Deploy reconciler.
The agent must create the core Kubernetes resources needed for a CockroachDB cluster:
- Discovery Service (headless)
- Public Service
- StatefulSet with persistent storage
- PodDisruptionBudget

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/deploy/
"""

import logging

from sregym.conductor.oracles.cockroachdb_deploy_oracle import CockroachDBDeployOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_DEPLOY_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBDeployApp:
    """
    Lightweight app class for CockroachDB Deploy benchmark.

    This app does NOT deploy any resources in deploy() - the preconditions
    (RBAC + CR) are set up in inject_fault() instead, following the
    incident management pattern where faults are injected after NOOP stage.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-deploy-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Deploy Benchmark - operator Deploy action simulation"

    def deploy(self):
        """
        No-op deployment.

        Preconditions are set up in inject_fault() instead, which is called
        by conductor after NOOP stage and before agent starts working.
        """
        local_logger.info(f"[Deploy Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent creates the workload resources."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Deploy Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBDeploy(Problem):
    """
    CockroachDB Deploy benchmark problem.

    Simulates the operator's Deploy action which creates core workload resources
    for a CockroachDB cluster after RBAC setup is complete.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up preconditions (RBAC + CR)
    4. Agent creates workload resources (Services, StatefulSet, PDB)
    5. Oracle validates the resources
    """

    def __init__(self):
        self.app = CockroachDBDeployApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBDeployOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Act as CockroachDB operator's Deploy reconciler. Create the core Kubernetes resources:

1. Discovery Service (headless) - for internal cluster communication
   - Name: crdb-cluster
   - clusterIP: None
   - Ports: grpc=26257, http=8080
   - Selector: app.kubernetes.io/name=cockroachdb, app.kubernetes.io/instance=crdb-cluster

2. Public Service - for client connections
   - Name: crdb-cluster-public
   - Type: ClusterIP
   - Same ports and selector as discovery service

3. StatefulSet - 3 CockroachDB pods
   - Name: crdb-cluster
   - Replicas: 3 (from spec.nodes)
   - ServiceName: crdb-cluster (points to discovery service)
   - ServiceAccount: crdb-cluster-sa
   - Container image: from spec.image.name
   - VolumeClaimTemplate: from spec.dataStore.pvc
   - Resource requests/limits: from spec.resources

4. PodDisruptionBudget - for high availability
   - Name: crdb-cluster-budget
   - MinAvailable: 1 (for 3 nodes)
   - Selector: matches StatefulSet pods

Read the CrdbCluster CR 'crdb-cluster' in namespace 'cockroachdb' for configuration.
The cluster requires 3 nodes with insecure mode (tlsEnabled=false)."""

    @mark_fault_injected
    def inject_fault(self):
        """
        Set up preconditions for the benchmark.

        This is called by conductor after NOOP stage, before agent starts working.

        Creates:
        - Namespace
        - CockroachDB CRDs
        - RBAC resources (ServiceAccount, Role, RoleBinding)
        - CrdbCluster CR

        Does NOT create workload resources - that's the agent's job.
        """
        local_logger.info(f"\n[Deploy Benchmark] Setting up preconditions...")

        # 1. Create namespace
        local_logger.info(f"  [1/4] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            if "AlreadyExists" in result:
                local_logger.info(f"    ℹ Namespace already exists")
            else:
                local_logger.info(f"    ✓ Namespace created")
        except Exception as e:
            local_logger.info(f"    ℹ Namespace may already exist: {e}")

        # 2. Install CockroachDB CRDs
        local_logger.info(f"  [2/4] Installing CockroachDB CRDs...")
        crd_url = "https://raw.githubusercontent.com/cockroachdb/cockroach-operator/master/install/crds.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {crd_url}")
        local_logger.info(f"    ✓ CRDs installed")

        # 2.5. Delete validating webhook (we don't have operator running)
        local_logger.info(f"  [2.5/4] Removing validating webhook (no operator in this benchmark)...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    ✓ Webhook removed")
        except Exception as e:
            local_logger.info(f"    ℹ Webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/4] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_DEPLOY_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create CrdbCluster CR
        local_logger.info(f"  [4/4] Creating CrdbCluster CR...")
        cr_path = f"{COCKROACH_DB_DEPLOY_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created")

        # Verify initial state - no workload resources should exist
        local_logger.info(f"\n[Deploy Benchmark] Verifying preconditions...")
        result = self.kubectl.exec_command(
            f"kubectl -n {self.namespace} get statefulset,service,pdb 2>&1 | grep -v NAME || echo 'No workload resources found'"
        )
        if "No workload resources found" in result or "No resources found" in result:
            local_logger.info(f"  ✓ No workload resources exist (expected)")
        else:
            local_logger.info(f"  ⚠ Warning: Some workload resources may already exist")

        local_logger.info(f"\n[Deploy Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Create workload resources by reading CrdbCluster CR '{self.cluster_name}'")
        local_logger.info(f"Expected resources to create:")
        local_logger.info(f"  1. Service/{self.cluster_name} (headless)")
        local_logger.info(f"  2. Service/{self.cluster_name}-public")
        local_logger.info(f"  3. StatefulSet/{self.cluster_name}")
        local_logger.info(f"  4. PodDisruptionBudget/{self.cluster_name}-budget\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup() which is called by conductor
        pass
