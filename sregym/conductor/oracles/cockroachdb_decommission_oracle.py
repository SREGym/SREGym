"""Oracle for validating CockroachDB Decommission operator action.

This oracle verifies that the agent successfully decommissioned nodes from a CockroachDB cluster,
simulating the operator's Decommission reconciler.

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/decommission/
"""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBDecommissionOracle(Oracle):
    """
    Oracle that validates successful decommissioning of CockroachDB cluster nodes.

    This oracle verifies that the agent:
    1. Executed "cockroach node decommission" for nodes to be removed
    2. Waited for decommission to complete (data migrated off nodes)
    3. Scaled down StatefulSet to target replica count
    4. Cluster remains healthy with remaining nodes
    5. Optionally cleaned up PVCs for decommissioned nodes

    The decommissioning is verified by checking:
    - Node status shows correct number of nodes
    - Decommissioned nodes are marked as decommissioned
    - StatefulSet has correct replica count
    - Cluster is functional (SQL queries work)
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether CockroachDB cluster nodes have been correctly decommissioned.

        Returns:
            dict: Results dictionary with 'success' (bool) and 'issues' (list) keys
        """
        print("== CockroachDB Decommission Oracle Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name
        results = {"success": True, "issues": []}

        # Expected state after decommission: 3 nodes (scaled down from 5)
        expected_nodes = 3
        expected_replicas = 3

        # Step 1: Check StatefulSet has been scaled down
        print(f"\n[1/5] Checking StatefulSet replica count...")
        try:
            sts = kubectl.apps_v1_api.read_namespaced_stateful_set(name=cluster_name, namespace=namespace)

            if sts.spec.replicas != expected_replicas:
                issue = f"StatefulSet replicas should be {expected_replicas}, got {sts.spec.replicas}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ StatefulSet scaled down to {expected_replicas} replicas")

            # Check ready replicas
            if sts.status.ready_replicas != expected_replicas:
                issue = f"Expected {expected_replicas} ready replicas, got {sts.status.ready_replicas}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ All {expected_replicas} replicas are ready")

        except ApiException as e:
            issue = f"Error checking StatefulSet: {e.reason}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False
            return results
        except Exception as e:
            issue = f"Error checking StatefulSet: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False
            return results

        # Step 2: Verify only expected pods exist
        print(f"\n[2/5] Checking pod count...")
        try:
            pods = kubectl.core_v1_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name=cockroachdb,app.kubernetes.io/instance={cluster_name}",
            )

            if len(pods.items) != expected_replicas:
                issue = f"Expected {expected_replicas} pods, found {len(pods.items)}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ Correct number of pods: {expected_replicas}")

            # Verify pod names (should be 0, 1, 2)
            pod_names = [pod.metadata.name for pod in pods.items]
            expected_pod_names = [f"{cluster_name}-{i}" for i in range(expected_replicas)]

            for expected_name in expected_pod_names:
                if expected_name not in pod_names:
                    issue = f"Expected pod '{expected_name}' not found"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)
                    results["success"] = False

            # Check for unexpected pods (3, 4, etc.)
            unexpected_pods = [name for name in pod_names if name not in expected_pod_names]
            if unexpected_pods:
                issue = f"Unexpected pods still exist: {', '.join(unexpected_pods)}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ No unexpected pods (decommissioned pods removed)")

        except ApiException as e:
            issue = f"Error checking pods: {e.reason}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False
        except Exception as e:
            issue = f"Error checking pods: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Step 3: Check cluster membership (node status)
        print(f"\n[3/5] Checking cluster node status...")
        try:
            node_status_cmd = (
                f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach node status --insecure --format=csv"
            )
            node_status_output = kubectl.exec_command(node_status_cmd)

            # Parse CSV output to count live nodes
            lines = node_status_output.strip().split("\n")
            # First line is header, rest are nodes
            node_lines = [line for line in lines if line and not line.startswith("id,")]

            # Count live nodes (is_live column should be true)
            live_nodes = 0
            decommissioned_nodes = 0
            for line in node_lines:
                fields = line.split(",")
                if len(fields) >= 6:  # Ensure we have enough fields
                    # is_live is typically the 5th column (index 4)
                    # is_decommissioning is typically the 6th column (index 5)
                    is_live = fields[4].strip().lower() == "true"
                    is_decommissioning = fields[5].strip().lower() == "true"

                    if is_live:
                        live_nodes += 1
                    if is_decommissioning:
                        decommissioned_nodes += 1

            if live_nodes != expected_nodes:
                issue = f"Expected {expected_nodes} live nodes, found {live_nodes}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ Cluster has {expected_nodes} live nodes")

            # Check that decommissioned nodes are properly marked
            # We decommissioned 2 nodes (from 5 to 3)
            expected_decommissioned = 2
            if decommissioned_nodes < expected_decommissioned:
                # This might be OK if nodes are fully removed, but log it
                print(
                    f"  ℹ️  Found {decommissioned_nodes} decommissioned nodes (expected {expected_decommissioned}, but may be fully removed)"
                )
            else:
                print(f"  ✅ Decommissioned nodes properly marked: {decommissioned_nodes}")

        except Exception as e:
            issue = f"Failed to check node status: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Step 4: Test SQL connectivity to ensure cluster is functional
        print(f"\n[4/5] Testing SQL connectivity...")
        try:
            sql_test_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT 1;'"
            sql_output = kubectl.exec_command(sql_test_cmd)

            if "ERROR" in sql_output or "error" in sql_output.lower():
                issue = f"SQL query failed: {sql_output}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ Cluster is functional (SQL queries working)")

        except Exception as e:
            issue = f"Failed to test SQL connectivity: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Step 5: Check PVC cleanup (optional but good practice)
        print(f"\n[5/5] Checking PVC cleanup...")
        try:
            pvcs = kubectl.core_v1_api.list_namespaced_persistent_volume_claim(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name=cockroachdb,app.kubernetes.io/instance={cluster_name}",
            )

            # Should have exactly expected_replicas PVCs (for pods 0, 1, 2)
            if len(pvcs.items) > expected_replicas:
                # Extra PVCs exist - this is not critical but worth noting
                issue = f"Found {len(pvcs.items)} PVCs, expected {expected_replicas}. Extra PVCs should be cleaned up."
                print(f"  ⚠️  {issue}")
                results["issues"].append(issue)
                # Don't fail the test for this, as PVC cleanup might be optional
                # results["success"] = False
            elif len(pvcs.items) == expected_replicas:
                print(f"  ✅ PVCs cleaned up correctly ({expected_replicas} remaining)")
            else:
                issue = f"Found {len(pvcs.items)} PVCs, expected {expected_replicas}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False

        except ApiException as e:
            issue = f"Error checking PVCs: {e.reason}"
            print(f"  ⚠️  {issue}")
            results["issues"].append(issue)
        except Exception as e:
            issue = f"Error checking PVCs: {str(e)}"
            print(f"  ⚠️  {issue}")
            results["issues"].append(issue)

        # Final summary
        print(f"\n{'='*60}")
        if results["success"]:
            print("✅ Decommission successful: Cluster scaled down safely from 5 to 3 nodes")
        else:
            print(f"❌ Decommission failed: {len(results['issues'])} issue(s) found")
            for i, issue in enumerate(results["issues"], 1):
                print(f"   {i}. {issue}")
        print(f"{'='*60}\n")

        return results
