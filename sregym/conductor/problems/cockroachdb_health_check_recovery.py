"""
CockroachDB Health Check Recovery Benchmark - Recovering from pod health check failures.

This benchmark tests an agent's ability to detect and recover from pod health check
failures that leave the cluster in an unhealthy state.

The agent must:
1. Identify pods with failing health checks
2. Diagnose the cause (network issue, process issue, etc.)
3. Execute recovery actions (restart pod, fix configuration, etc.)
4. Verify cluster recovers to healthy state
5. Ensure no underreplicated ranges remain
6. Remove the annotation when complete

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/health-check-recovery/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_health_check_recovery_oracle import CockroachDBHealthCheckRecoveryOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_HEALTH_CHECK_RECOVERY_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBHealthCheckRecoveryApp:
    """
    Lightweight app class for CockroachDB Health Check Recovery benchmark.

    This app does NOT deploy resources in deploy() - the preconditions
    are set up in inject_fault() instead.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-health-check-recovery-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Health Check Recovery Benchmark - recovering from pod health check failures"

    def deploy(self):
        """
        No-op deployment.

        Preconditions (3-node cluster with 1 failed healthcheck) are set up in inject_fault().
        """
        local_logger.info(f"[Health Check Recovery Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent recovers failed pod."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Health Check Recovery Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  Cleanup complete")


class CockroachDBHealthCheckRecovery(Problem):
    """
    CockroachDB Health Check Recovery benchmark problem.

    Simulates the operator's HealthCheckRecovery action which detects and recovers
    from pod health check failures in the CockroachDB cluster.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up 3-node cluster, fails 1 healthcheck
    4. Agent detects annotation: crdb.cockroachlabs.com/health-check=required
    5. Agent identifies pod with failing health checks
    6. Agent diagnoses and executes recovery action
    7. Agent verifies pod recovers and cluster returns to healthy state
    8. Agent removes annotation
    9. Oracle validates pod recovered, cluster healthy, no underreplicated ranges
    """

    def __init__(self):
        self.app = CockroachDBHealthCheckRecoveryApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBHealthCheckRecoveryOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Act as CockroachDB operator's HealthCheckRecovery reconciler. Recover from pod health check failures.

OBSERVED STATE:
- 3-node CockroachDB cluster was healthy
- CrdbCluster CR has annotation: crdb.cockroachlabs.com/health-check=required
- One pod has failing readiness/liveness checks (pod not ready)
- Cluster may have underreplicated ranges due to unavailable node
- Reduced quorum operation

DESIRED STATE:
- Pod with failing health checks recovered and running
- Pod passes readiness and liveness checks
- Cluster returns to healthy state with all nodes available
- All ranges properly replicated (no underreplicated ranges)
- Annotation removed from CrdbCluster CR
- Cluster fully operational with 3 healthy nodes

YOUR TASK:
Diagnose and recover the pod that is failing health checks.

CONSTRAINTS:
- Identify which pod has failing health checks
- Diagnose the root cause
- Execute recovery action (may require pod restart or log investigation)
- Verify pod returns to running and ready state
- Ensure cluster rebalances and all ranges are replicated
- Remove annotation when complete

IMPORTANT CONCEPTS:
- Kubernetes health checks: readiness and liveness probes
- Failing probes prevent pod from receiving traffic or restart pod
- CockroachDB requires quorum for operations (N/2+1 nodes)
- Unavailable nodes trigger range replication issues
- Recovery may require investigating logs, restarting pods, etc.
- Cluster self-heals once node rejoins

Read pod status in namespace 'cockroachdb' to find the failed pod.
Check pod logs to diagnose the health check failure.
Execute recovery action (restart, clear state, etc.) as needed.
Verify cluster health returns via SQL commands."""

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
        - Creates CrdbCluster CR with health-check annotation
        - Simulates health check failure on one pod

        The "fault" here is the failing health check on one pod.
        """
        local_logger.info(f"\n[Health Check Recovery Benchmark] Setting up preconditions...")

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
        rbac_path = f"{COCKROACH_DB_HEALTH_CHECK_RECOVERY_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/9] Creating Services...")
        services_path = f"{COCKROACH_DB_HEALTH_CHECK_RECOVERY_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    Services created")

        # 5. Create StatefulSet with 3 replicas
        local_logger.info(f"  [5/9] Creating StatefulSet with 3 replicas...")
        sts_path = f"{COCKROACH_DB_HEALTH_CHECK_RECOVERY_RESOURCES}/statefulset.yaml"
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

        # 8. Create CrdbCluster CR with health-check annotation
        local_logger.info(f"  [8/9] Creating CrdbCluster CR with health-check annotation...")
        cr_path = f"{COCKROACH_DB_HEALTH_CHECK_RECOVERY_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    CrdbCluster CR created with health-check annotation")

        # 9. Simulate health check failure on one pod
        local_logger.info(f"  [9/9] Simulating health check failure on pod {self.cluster_name}-2...")
        try:
            # Modify the pod to fail readiness checks by corrupting health check endpoint
            # This is a simulation - in real scenario this would be caused by actual issues
            fail_cmd = f"kubectl -n {self.namespace} exec {self.cluster_name}-2 -- touch /tmp/fail_health_check"
            result = self.kubectl.exec_command(fail_cmd)
            local_logger.info(f"    Health check failure simulated on pod-2")
        except Exception as e:
            local_logger.info(f"    Warning: Could not simulate health check failure: {e}")

        # Verify preconditions
        local_logger.info(f"\n[Health Check Recovery Benchmark] Verifying preconditions...")
        try:
            # Check annotation exists
            annotation_cmd = f"kubectl -n {self.namespace} get crdbcluster {self.cluster_name} -o jsonpath='{{.metadata.annotations.crdb\\.cockroachlabs\\.com/health-check}}'"
            annotation = self.kubectl.exec_command(annotation_cmd)
            local_logger.info(f"  Annotation value: {annotation}")
            if annotation == "required":
                local_logger.info(f"  Annotation detected (fault injected)")
            else:
                local_logger.info(f"  Warning: Annotation may not be correct")
        except Exception as e:
            local_logger.info(f"  Warning: Could not verify annotation: {e}")

        local_logger.info(f"\n[Health Check Recovery Benchmark] Preconditions complete!")
        local_logger.info(f"\nAgent task: Recover from pod health check failure")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Identify pod with failing health checks (pod-2)")
        local_logger.info(f"  2. Check pod logs to diagnose the issue")
        local_logger.info(f"  3. Execute recovery action (restart pod)")
        local_logger.info(f"  4. Verify pod becomes ready and healthy")
        local_logger.info(f"  5. Confirm cluster health and all ranges replicated")
        local_logger.info(f"  6. Remove the health-check annotation\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup()
        pass
