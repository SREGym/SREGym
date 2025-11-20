"""
CockroachDB Expose Ingress Benchmark - Creating Ingress resources for external access.

This benchmark tests an agent's ability to create and configure Ingress resources
for exposing a CockroachDB cluster for external access.

The agent must:
1. Create Ingress resources pointing to the service
2. Configure appropriate routing rules
3. Set up TLS configuration (if required)
4. Verify Ingress is properly configured
5. Remove the annotation when complete

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/expose-ingress/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_expose_ingress_oracle import CockroachDBExposeIngressOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_EXPOSE_INGRESS_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBExposeIngressApp:
    """
    Lightweight app class for CockroachDB Expose Ingress benchmark.

    This app does NOT deploy resources in deploy() - the preconditions
    are set up in inject_fault() instead.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-expose-ingress-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Expose Ingress Benchmark - creating Ingress resources for external access"

    def deploy(self):
        """
        No-op deployment.

        Preconditions (3-node cluster) are set up in inject_fault().
        """
        local_logger.info(f"[Expose Ingress Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent creates Ingress resources."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Expose Ingress Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  Cleanup complete")


class CockroachDBExposeIngress(Problem):
    """
    CockroachDB Expose Ingress benchmark problem.

    Simulates the operator's ExposeIngress action which creates Ingress resources
    to expose the CockroachDB cluster for external access.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up 3-node cluster with annotation
    4. Agent detects annotation: crdb.cockroachlabs.com/expose-ingress=required
    5. Agent creates Ingress resources pointing to the public service
    6. Agent configures routing rules and TLS
    7. Agent verifies Ingress is properly configured
    8. Agent removes annotation
    9. Oracle validates Ingress created, service configured, TLS setup
    """

    def __init__(self):
        self.app = CockroachDBExposeIngressApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBExposeIngressOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Act as CockroachDB operator's ExposeIngress reconciler. Create Ingress resources for external access.

OBSERVED STATE:
- 3-node CockroachDB cluster is running and healthy
- Service 'crdb-cluster-public' exists in the cluster
- CrdbCluster CR has annotation: crdb.cockroachlabs.com/expose-ingress=required
- No Ingress resources exist yet

DESIRED STATE:
- Ingress resource created for external access
- Ingress routes traffic to 'crdb-cluster-public' service
- TLS configuration in place (with appropriate certificates/configuration)
- Ingress is accessible and properly configured
- Annotation removed from CrdbCluster CR
- Cluster remains operational

YOUR TASK:
Create Ingress resources to expose the CockroachDB cluster for external access.

CONSTRAINTS:
- Create Ingress resource targeting crdb-cluster-public service
- Configure routing rules (path-based or host-based)
- Set up TLS configuration for secure access
- Verify Ingress is properly recognized by the cluster
- Remove annotation when complete

IMPORTANT CONCEPTS:
- Ingress resources expose HTTP/HTTPS routes from outside the cluster
- Ingress requires an Ingress Controller (may already be present)
- TLS can be configured with certificates
- Ingress must reference an existing Service
- Host or path rules determine where traffic is routed

Read the Service 'crdb-cluster-public' in namespace 'cockroachdb'.
Create an Ingress resource that exposes this service with appropriate TLS configuration.
Update annotation when Ingress creation is complete."""

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
        - Creates CrdbCluster CR with expose-ingress annotation

        The "fault" here is the missing Ingress resource.
        """
        local_logger.info(f"\n[Expose Ingress Benchmark] Setting up preconditions...")

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
        local_logger.info(f"  [2.5/9] Removing validating webhook...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    Webhook removed")
        except Exception as e:
            local_logger.info(f"    Webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/9] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_EXPOSE_INGRESS_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/9] Creating Services...")
        services_path = f"{COCKROACH_DB_EXPOSE_INGRESS_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    Services created")

        # 5. Create StatefulSet with 3 replicas
        local_logger.info(f"  [5/9] Creating StatefulSet with 3 replicas...")
        sts_path = f"{COCKROACH_DB_EXPOSE_INGRESS_RESOURCES}/statefulset.yaml"
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

        # 8. Create CrdbCluster CR with expose-ingress annotation
        local_logger.info(f"  [8/9] Creating CrdbCluster CR with expose-ingress annotation...")
        cr_path = f"{COCKROACH_DB_EXPOSE_INGRESS_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    CrdbCluster CR created with expose-ingress annotation")

        # 9. Verify preconditions
        local_logger.info(f"\n[Expose Ingress Benchmark] Verifying preconditions...")
        try:
            # Check annotation exists
            annotation_cmd = f"kubectl -n {self.namespace} get crdbcluster {self.cluster_name} -o jsonpath='{{.metadata.annotations.crdb\\.cockroachlabs\\.com/expose-ingress}}'"
            annotation = self.kubectl.exec_command(annotation_cmd)
            local_logger.info(f"  Annotation value: {annotation}")
            if annotation == "required":
                local_logger.info(f"  Annotation detected (fault injected)")
            else:
                local_logger.info(f"  Warning: Annotation may not be correct")
        except Exception as e:
            local_logger.info(f"  Warning: Could not verify annotation: {e}")

        local_logger.info(f"\n[Expose Ingress Benchmark] Preconditions complete!")
        local_logger.info(f"\nAgent task: Create Ingress resources for external access")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Create Ingress resource")
        local_logger.info(f"  2. Configure routing to crdb-cluster-public service")
        local_logger.info(f"  3. Set up TLS configuration")
        local_logger.info(f"  4. Verify Ingress is recognized and accessible")
        local_logger.info(f"  5. Remove the expose-ingress annotation\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup()
        pass
