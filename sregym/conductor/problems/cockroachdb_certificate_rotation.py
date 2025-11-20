"""
CockroachDB Certificate Rotation Benchmark - Rotating TLS certificates with zero downtime.

This benchmark tests an agent's ability to rotate TLS certificates for a CockroachDB
cluster while maintaining availability and zero downtime.

The agent must:
1. Generate new TLS certificates
2. Update certificate secrets
3. Restart pods with new certificates
4. Verify cluster remains available throughout rotation
5. Ensure all pods are using new certificates

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/certificate-rotation/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_certificate_rotation_oracle import CockroachDBCertificateRotationOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_CERTIFICATE_ROTATION_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBCertificateRotationApp:
    """
    Lightweight app class for CockroachDB Certificate Rotation benchmark.

    This app does NOT deploy resources in deploy() - the preconditions
    are set up in inject_fault() instead.
    """

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-certificate-rotation-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Certificate Rotation Benchmark"

    def deploy(self):
        """
        No-op deployment.

        Preconditions are set up in inject_fault().
        """
        local_logger.info(f"[Certificate Rotation Benchmark] App deploy() called - no action needed")
        local_logger.info(f"  Preconditions will be set up in inject_fault() after NOOP stage")

    def start_workload(self):
        """No workload to start - agent performs certificate rotation."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Certificate Rotation Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBCertificateRotation(Problem):
    """
    CockroachDB Certificate Rotation benchmark problem.

    Simulates the operator's certificate rotation workflow.

    Flow:
    1. Conductor calls app.deploy() - does nothing
    2. Agent submits NOOP detection
    3. Conductor calls inject_fault() - sets up 3-node TLS-enabled cluster
    4. Agent rotates TLS certificates
    5. Agent restarts pods with new certificates
    6. Oracle validates new certs generated, pods restarted, cluster healthy
    """

    def __init__(self):
        self.app = CockroachDBCertificateRotationApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBCertificateRotationOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Rotate TLS certificates for CockroachDB cluster with zero downtime.

OBSERVED STATE:
- CockroachDB cluster running with TLS enabled (3 nodes)
- Current TLS certificates nearing expiration
- CR has annotation: crdb.cockroachlabs.com/rotate-certs=required
- Cluster is healthy with all pods running
- TLS secret exists but certificates will expire soon

DESIRED STATE:
- New TLS certificates generated
- Certificate secret updated with new certs
- All pods restarted with new certificates
- Cluster remains healthy throughout rotation (zero downtime)
- All pods using new certificates
- Cluster maintains quorum during rotation

YOUR TASK:
Rotate TLS certificates for the CockroachDB cluster without causing downtime.

CONSTRAINTS:
- Maintain cluster availability throughout (zero downtime)
- Update pods one at a time (rolling restart)
- Verify cluster health after each pod restart
- Ensure new certificates are loaded by all pods
- Keep cluster quorum maintained during rotation

IMPORTANT CONCEPTS:
- CockroachDB TLS uses CA certificate, node certificates, and client certificates
- Certificate rotation requires pod restarts to reload certs
- Rolling restart strategy maintains availability (N-1 nodes available)
- Health checks ensure pods are ready before next rotation

Read the CrdbCluster CR 'crdb-cluster' in namespace 'cockroachdb' for cert rotation status.
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
        - StatefulSet with TLS enabled, 3 replicas
        - Initializes the cluster with TLS
        - Creates TLS secret (initial certificates)
        - Creates CrdbCluster CR requesting certificate rotation
        """
        local_logger.info(f"\n[Certificate Rotation Benchmark] Setting up preconditions...")

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
        rbac_path = f"{COCKROACH_DB_CERTIFICATE_ROTATION_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/9] Creating Services...")
        services_path = f"{COCKROACH_DB_CERTIFICATE_ROTATION_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 5. Create StatefulSet with TLS enabled
        local_logger.info(f"  [5/9] Creating StatefulSet with TLS enabled (3 replicas)...")
        sts_path = f"{COCKROACH_DB_CERTIFICATE_ROTATION_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    ✓ StatefulSet created")

        # Wait for first pod
        local_logger.info(f"  [5.5/9] Waiting for first pod to be running...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=jsonpath='{{.status.phase}}'=Running pod/{self.cluster_name}-0 --timeout=300s"
            )
            local_logger.info(f"    ✓ Pod {self.cluster_name}-0 is running")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Pod may not be running yet: {e}")

        local_logger.info(f"  [5.6/9] Waiting 30 seconds for CockroachDB process to start...")
        time.sleep(30)
        local_logger.info(f"    ✓ Wait complete")

        # 6. Initialize the cluster
        local_logger.info(f"  [6/9] Initializing the 3-node cluster...")
        max_retries = 5
        for attempt in range(max_retries):
            try:
                init_cmd = f"kubectl -n {self.namespace} exec {self.cluster_name}-0 -- ./cockroach init --certs-dir=/cockroach/certs"
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

        # 7. Create initial TLS secret (simulating existing certificates)
        local_logger.info(f"  [7/9] Creating TLS secret with initial certificates...")
        try:
            cert_secret = f"kubectl -n {self.namespace} create secret tls crdb-tls-certs --cert=/dev/null --key=/dev/null 2>/dev/null || true"
            result = self.kubectl.exec_command(cert_secret)
            local_logger.info(f"    ✓ TLS secret created")
        except Exception as e:
            local_logger.info(f"    ℹ TLS secret may already exist: {e}")

        # 8. Create CrdbCluster CR requesting certificate rotation
        local_logger.info(f"  [8/9] Creating CrdbCluster CR...")
        cr_path = f"{COCKROACH_DB_CERTIFICATE_ROTATION_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created with cert rotation annotation")

        # 9. Verify preconditions
        local_logger.info(f"\n[Certificate Rotation Benchmark] Verifying preconditions...")
        try:
            cr_cmd = f"kubectl -n {self.namespace} get crdbcluster {self.cluster_name} -o jsonpath='{{.metadata.annotations.crdb\\.cockroachlabs\\.com/rotate-certs}}'"
            cr_cert = self.kubectl.exec_command(cr_cmd)
            local_logger.info(f"  ✓ CrdbCluster CR has cert rotation annotation: {cr_cert}")
        except Exception as e:
            local_logger.info(f"  ⚠️  Warning: Could not verify cert rotation annotation: {e}")

        local_logger.info(f"\n[Certificate Rotation Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Rotate TLS certificates with zero downtime")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Generate new TLS certificates")
        local_logger.info(f"  2. Update the TLS secret with new certificates")
        local_logger.info(f"  3. Restart pods one at a time (rolling restart)")
        local_logger.info(f"  4. Verify cluster health after each pod restart")
        local_logger.info(f"  5. Confirm all pods using new certificates\n")

    @mark_fault_injected
    def recover_fault(self):
        """
        Clean up all resources created during the benchmark.

        This is called by conductor at the end of the problem.
        """
        # No-op - cleanup is handled by app.cleanup()
        pass
