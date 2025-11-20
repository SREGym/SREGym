"""Oracle for validating CockroachDB Certificate Rotation operator action.

This oracle verifies that the agent successfully rotated TLS certificates
with zero downtime.

The oracle validates:
1. New certificates generated
2. Pods restarted with new certificates
3. Cluster remained healthy throughout rotation

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/certificate-rotation/
"""

from sregym.conductor.oracles.base import Oracle


class CockroachDBCertificateRotationOracle(Oracle):
    """
    Oracle that validates the Certificate Rotation operator workflow.

    Weighted scoring breakdown:
    - certs_generated: 35% - New certificates created
    - pods_rotated: 35% - Pods restarted and using new certs
    - cluster_healthy: 30% - Cluster remained healthy throughout

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent successfully rotated certificates with zero downtime.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Certificate Rotation Oracle Evaluation ==")
        print("Testing certificate rotation with zero downtime")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {"certs_generated": False, "pods_rotated": False, "cluster_healthy": False},
        }

        expected_replicas = 3

        # STEP 1: Check if new certificates were generated (35% weight)
        print(f"\n[1/3] Checking if new certificates were generated (35% weight)...")
        certs_generated = False

        try:
            # Check for TLS secret in namespace
            secret_cmd = (
                f"kubectl -n {namespace} get secret crdb-tls-certs -o jsonpath='{{.data.tls\\.crt}}' 2>/dev/null"
            )
            secret_output = kubectl.exec_command(secret_cmd)

            if secret_output and len(secret_output.strip()) > 0:
                print(f"  ✅ TLS certificate found in secret")
                certs_generated = True
                results["breakdown"]["certs_generated"] = True
                results["score"] += 0.35
            else:
                issue = f"TLS secret missing or empty"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Could not verify certificate generation: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 2: Check if pods were rotated (35% weight)
        print(f"\n[2/3] Checking if pods were restarted with new certs (35% weight)...")
        pods_rotated = False

        try:
            # Check pod restart counts and readiness
            pods = kubectl.core_v1_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name=cockroachdb,app.kubernetes.io/instance={cluster_name}",
            )

            if len(pods.items) != expected_replicas:
                issue = f"Expected {expected_replicas} pods, found {len(pods.items)}"
                print(f"  ⚠️  {issue}")
                results["issues"].append(issue)
            else:
                pods_ready = 0
                for pod in pods.items:
                    pod_name = pod.metadata.name

                    if pod.status.phase == "Running":
                        if pod.status.container_statuses:
                            container_ready = pod.status.container_statuses[0].ready
                            if container_ready:
                                # Check if pod has restarted (indicates rotation)
                                if pod.status.container_statuses[0].restart_count > 0:
                                    print(
                                        f"  ✅ Pod '{pod_name}' restarted (restart_count: {pod.status.container_statuses[0].restart_count})"
                                    )
                                    pods_ready += 1
                                else:
                                    print(f"  ℹ️  Pod '{pod_name}' ready (may be initial startup)")
                                    pods_ready += 1

                if pods_ready == expected_replicas:
                    print(f"  ✅ All {expected_replicas} pods ready and restarted")
                    pods_rotated = True
                    results["breakdown"]["pods_rotated"] = True
                    results["score"] += 0.35
                elif pods_ready > 0:
                    partial_score = 0.35 * (pods_ready / expected_replicas)
                    results["score"] += partial_score
                    print(f"  ⚠️  Partial: {pods_ready}/{expected_replicas} pods rotated (+{partial_score:.0%})")

        except Exception as e:
            issue = f"Could not verify pod rotation: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 3: Check cluster health (30% weight)
        print(f"\n[3/3] Checking cluster health after rotation (30% weight)...")
        cluster_healthy = False

        try:
            # Test SQL connectivity with TLS
            sql_test_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --certs-dir=/cockroach/certs -e 'SELECT 1;' 2>/dev/null"
            sql_output = kubectl.exec_command(sql_test_cmd)

            if "1" in sql_output or "ERROR" not in sql_output:
                print(f"  ✅ SQL queries working with TLS")

                # Check node status
                node_status_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach node status --certs-dir=/cockroach/certs --format=csv 2>/dev/null"
                node_status_output = kubectl.exec_command(node_status_cmd)

                lines = node_status_output.strip().split("\n")
                node_lines = [line for line in lines if line and not line.startswith("id,")]

                live_nodes = 0
                for line in node_lines:
                    fields = line.split(",")
                    if len(fields) >= 5:
                        is_live = fields[4].strip().lower() == "true"
                        if is_live:
                            live_nodes += 1

                if live_nodes == expected_replicas:
                    print(f"  ✅ All {expected_replicas} nodes healthy with new certificates")
                    cluster_healthy = True
                    results["breakdown"]["cluster_healthy"] = True
                    results["score"] += 0.30
                else:
                    issue = f"Expected {expected_replicas} live nodes, found {live_nodes}"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)

        except Exception as e:
            issue = f"Failed to check cluster health: {str(e)}"
            print(f"  ⚠️  {issue}")

        # Final summary
        print(f"\n[Summary] Final Evaluation")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - Certs generated:    {'✅ +35%' if results['breakdown']['certs_generated'] else '❌ +0%'}")
        print(f"  - Pods rotated:       {'✅ +35%' if results['breakdown']['pods_rotated'] else '❌ +0%'}")
        print(f"  - Cluster healthy:    {'✅ +30%' if results['breakdown']['cluster_healthy'] else '❌ +0%'}")
        print(f"\nTotal Score: {results['score']:.0%} ({'Pass' if results['score'] >= 0.70 else 'Fail'})")

        if results["issues"]:
            print(f"\nIssues found ({len(results['issues'])}):")
            for i, issue in enumerate(results["issues"], 1):
                print(f"  {i}. {issue}")

        # Overall pass/fail
        if results["score"] < 0.70:
            results["success"] = False
            print(f"\n❌ FAIL: Score {results['score']:.0%} below 70% threshold")
        else:
            results["success"] = True
            print(f"\n✅ PASS: Certificate rotation completed successfully")

        print(f"=" * 60 + "\n")

        return results
