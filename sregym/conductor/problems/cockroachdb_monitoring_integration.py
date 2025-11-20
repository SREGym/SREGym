"""
CockroachDB Monitoring Integration Benchmark - Creating ServiceMonitor for Prometheus.

This benchmark tests an agent's ability to configure Prometheus monitoring
for a CockroachDB cluster using ServiceMonitor CRD.

The agent must:
1. Create ServiceMonitor CRD
2. Configure metrics scraping from CockroachDB
3. Verify metrics endpoint is accessible
4. Ensure Prometheus can scrape metrics
5. Test metric collection

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/monitoring-integration/
"""

import logging

from sregym.conductor.oracles.cockroachdb_monitoring_integration_oracle import CockroachDBMonitoringIntegrationOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import COCKROACH_DB_MONITORING_INTEGRATION_RESOURCES
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

local_logger = logging.getLogger("all.application")


class CockroachDBMonitoringIntegrationApp:
    """Lightweight app class for CockroachDB Monitoring Integration benchmark."""

    def __init__(self):
        self.kubectl = KubeCtl()
        self.namespace = "cockroachdb"
        self.name = "cockroachdb-monitoring-integration-benchmark"
        self.app_name = "cockroachdb"
        self.description = "CockroachDB Monitoring Integration Benchmark"

    def deploy(self):
        """No-op deployment."""
        local_logger.info(f"[Monitoring Integration Benchmark] App deploy() called - no action needed")

    def start_workload(self):
        """No workload to start."""
        pass

    def cleanup(self):
        """Delete the namespace and all resources."""
        local_logger.info(f"[Monitoring Integration Benchmark] Cleaning up namespace '{self.namespace}'...")
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        local_logger.info("  ✓ Cleanup complete")


class CockroachDBMonitoringIntegration(Problem):
    """CockroachDB Monitoring Integration benchmark problem."""

    def __init__(self):
        self.app = CockroachDBMonitoringIntegrationApp()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.cluster_name = "crdb-cluster"

        super().__init__(app=self.app, namespace=self.namespace)
        self.mitigation_oracle = CockroachDBMonitoringIntegrationOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return """Create ServiceMonitor for Prometheus integration with CockroachDB.

OBSERVED STATE:
- CockroachDB cluster running with 3 healthy nodes
- Metrics endpoint available on port 8080
- CR has annotation: crdb.cockroachlabs.com/monitoring=prometheus
- ServiceMonitor CRD not yet installed
- No metrics scraping configured

DESIRED STATE:
- ServiceMonitor CRD created
- ServiceMonitor configured to scrape CockroachDB metrics
- Prometheus can discover and scrape metrics endpoint
- Metrics are being collected and accessible
- All cluster metrics available for monitoring

YOUR TASK:
Set up Prometheus monitoring for the CockroachDB cluster.

CONSTRAINTS:
- ServiceMonitor must target correct metrics port (8080)
- Scrape interval should be reasonable (30s-60s)
- Include all CockroachDB metrics
- Ensure proper label selectors for service discovery

IMPORTANT CONCEPTS:
- ServiceMonitor is a CRD for Prometheus Operator
- CockroachDB exposes metrics on HTTP endpoint
- Metrics include cluster health, SQL stats, replication, etc.
- Proper scraping requires correct service and port config

Create ServiceMonitor and verify metrics are accessible."""

    @mark_fault_injected
    def inject_fault(self):
        """Set up preconditions for the benchmark."""
        local_logger.info(f"\n[Monitoring Integration Benchmark] Setting up preconditions...")

        # 1. Create namespace
        local_logger.info(f"  [1/7] Creating namespace '{self.namespace}'...")
        try:
            result = self.kubectl.exec_command(f"kubectl create namespace {self.namespace}")
            local_logger.info(f"    ✓ Namespace created")
        except Exception as e:
            local_logger.info(f"    ℹ Namespace may already exist: {e}")

        # 2. Install CockroachDB CRDs
        local_logger.info(f"  [2/7] Installing CockroachDB CRDs...")
        crd_url = "https://raw.githubusercontent.com/cockroachdb/cockroach-operator/master/install/crds.yaml"
        result = self.kubectl.exec_command(f"kubectl apply -f {crd_url}")
        local_logger.info(f"    ✓ CRDs installed")

        # 2.5. Delete validating webhook
        local_logger.info(f"  [2.5/7] Removing validating webhook...")
        try:
            result = self.kubectl.exec_command(
                "kubectl delete validatingwebhookconfiguration cockroach-operator-validating-webhook-configuration"
            )
            local_logger.info(f"    ✓ Webhook removed")
        except Exception as e:
            local_logger.info(f"    ℹ Webhook may not exist: {e}")

        # 3. Create RBAC resources
        local_logger.info(f"  [3/7] Creating RBAC resources...")
        rbac_path = f"{COCKROACH_DB_MONITORING_INTEGRATION_RESOURCES}/rbac.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {rbac_path}")
        local_logger.info(f"    ✓ RBAC resources created")

        # 4. Create Services
        local_logger.info(f"  [4/7] Creating Services...")
        services_path = f"{COCKROACH_DB_MONITORING_INTEGRATION_RESOURCES}/services.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {services_path}")
        local_logger.info(f"    ✓ Services created")

        # 5. Create StatefulSet
        local_logger.info(f"  [5/7] Creating StatefulSet...")
        sts_path = f"{COCKROACH_DB_MONITORING_INTEGRATION_RESOURCES}/statefulset.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {sts_path}")
        local_logger.info(f"    ✓ StatefulSet created")

        # 6. Wait for cluster to be ready
        local_logger.info(f"  [6/7] Waiting for cluster to be ready...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl -n {self.namespace} wait --for=condition=ready pod -l app.kubernetes.io/name=cockroachdb --timeout=600s"
            )
            local_logger.info(f"    ✓ Cluster ready")
        except Exception as e:
            local_logger.info(f"    ⚠️  Warning: Cluster may not be ready: {e}")

        # 7. Create CrdbCluster CR
        local_logger.info(f"  [7/7] Creating CrdbCluster CR...")
        cr_path = f"{COCKROACH_DB_MONITORING_INTEGRATION_RESOURCES}/crdb-cluster.yaml"
        result = self.kubectl.exec_command(f"kubectl -n {self.namespace} apply -f {cr_path}")
        local_logger.info(f"    ✓ CrdbCluster CR created")

        local_logger.info(f"\n[Monitoring Integration Benchmark] ✅ Preconditions complete!")
        local_logger.info(f"\n📋 Agent task: Create ServiceMonitor for Prometheus\n")

    @mark_fault_injected
    def recover_fault(self):
        """Clean up resources."""
        pass
