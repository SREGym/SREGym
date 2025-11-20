"""
CockroachDB Generate Cert Benchmark - Generating TLS certificates before deployment.

This benchmark tests an agent's ability to generate TLS certificates
before deploying a CockroachDB cluster.

The agent must:
1. Generate CA certificate
2. Generate node certificates for all nodes
3. Generate client certificate
4. Create TLS secret with all certificates
5. Deploy cluster with TLS enabled

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/generate-cert/
"""

import logging
import time

from sregym.conductor.oracles.cockroachdb_generate_cert_oracle import CockroachDBGenerateCertOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_GENERATE_CERT_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBGenerateCertApp:
    """Lightweight app class for CockroachDB Generate Cert benchmark."""

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-generate-cert-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Generate Cert Benchmark"

    def deploy(self):
        """No-op deployment - cert generation happens in inject_fault."""
        local_logger.info(f"[Generate Cert Benchmark] App deploy() called - no action needed")

    def start_workload(self):
        """No workload to start."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Generate Cert Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBGenerateCert(Problem):
    """
    CockroachDB Generate Cert benchmark problem.

    Simulates certificate generation before cluster deployment.
    """

    def __init__(self):
        self.app = CockroachDBGenerateCertApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBGenerateCertOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Generate TLS certificates for CockroachDB cluster deployment.

OBSERVED STATE:
- Namespace created but empty
- CrdbCluster CR present with tlsEnabled: true
- CR has annotation: crdb.cockroachlabs.com/generate-cert=required
- No TLS certificates generated yet
- No StatefulSet deployed yet

DESIRED STATE:
- CA certificate generated
- Node certificates created for all 3 nodes
- Client certificate created
- TLS secret created with all certificates
- Cluster ready to be deployed with TLS enabled

YOUR TASK:
Generate all required TLS certificates before deploying the CockroachDB cluster.

CONSTRAINTS:
- Generate CA cert with proper validity period
- Generate node certs for each cluster node (node1, node2, node3)
- Generate client certificate for administration
- Create Kubernetes secret with all certificates

IMPORTANT CONCEPTS:
- CockroachDB requires separate CA, node, and client certificates
- Certificates must be in PEM format
- Node certificates need FQDN for the StatefulSet headless service
- Client cert is used by cockroach CLI tools

Create the namespace, install CRDs, create RBAC, and generate certificates."""

    @mark_fault_injected
    def inject_fault(self):
        """
        Set up preconditions for the benchmark.
        """
        local_logger.info(f"\n[Generate Cert Benchmark] Setting up preconditions...")

        # 1. Create namespace
        local_logger.info(f"  [1/5] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            local_logger.info(f"    ✓ Namespace created")
        except Exception as e:
            local_logger.info(f"    ℹ Namespace may already exist: {e}")

        # 2. Install CockroachDB CRDs
        local_logger.info(f"  [2/5] Installing CockroachDB CRDs...")
        crd_url = "https://raw.githubusercontent.com/cockroachdb/cockroach-operator/master/install/crds.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {crd_url}")
        local_logger.info(f"    ✓ CRDs installed")

        # 2.5. Delete validating webhook
        local_logger.info(f"  [2.5/5] Removing validating webhook...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    ✓ Webhook removed")
        except Exception as e:
            local_logger.info(f"    ℹ Webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/5] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_GENERATE_CERT_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create CrdbCluster CR with TLS enabled
        local_logger.info(f"  [4/5] Creating CrdbCluster CR with TLS enabled...")
        cr_path = f"{COCKROACH_DB_GENERATE_CERT_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created")

        # 5. Create Services for certificate generation
        local_logger.info(f"  [5/5] Creating Services...")
        services_path = f"{COCKROACH_DB_GENERATE_CERT_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        local_logger.info(f"\n[Generate Cert Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Generate TLS certificates")
        local_logger.info(f"Expected workflow:")
        local_logger.info(f"  1. Generate CA certificate")
        local_logger.info(f"  2. Generate node certificates (node1, node2, node3)")
        local_logger.info(f"  3. Generate client certificate")
        local_logger.info(f"  4. Create TLS secret with all certificates\n")

    @mark_fault_injected
    def recover_fault(self):
        """Clean up resources."""
        pass
