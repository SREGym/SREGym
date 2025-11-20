"""Oracle for validating CockroachDB Initialize operator action.

This oracle verifies that the agent successfully initialized a CockroachDB cluster,
simulating the operator's Initialize reconciler.

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/initialize/
"""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBInitializeOracle(Oracle):
    """
    Oracle that validates successful initialization of CockroachDB cluster.

    This oracle verifies that the agent:
    1. Executed "cockroach init" command on the cluster
    2. Cluster can now accept SQL queries
    3. All nodes are visible and healthy in cluster membership
    4. Cluster is fully initialized and operational

    The initialization is verified by running "cockroach node status" command
    which should show all nodes as alive and healthy.
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether CockroachDB cluster has been correctly initialized.

        Returns:
            dict: Results dictionary with 'success' (bool) and 'issues' (list) keys
        """
        print("== CockroachDB Initialize Oracle Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name
        results = {"success": True, "issues": []}

        expected_nodes = 3

        # Step 1: Check if cluster is initialized by running "cockroach node status"
        print(f"\n[1/3] Checking cluster initialization status...")
        try:
            # Try to get node status - this only works if cluster is initialized
            node_status_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach node status --insecure"
            node_status_output = kubectl.exec_command(node_status_cmd)

            if "ERROR" in node_status_output or "error" in node_status_output.lower():
                issue = f"Cluster not initialized: {node_status_output}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ Cluster is initialized and responding to commands")

        except Exception as e:
            issue = f"Failed to check cluster initialization: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False
            # If we can't check initialization, no point checking other things
            return results

        # Step 2: Verify all expected nodes are in the cluster
        print(f"\n[2/3] Checking cluster membership (expected {expected_nodes} nodes)...")
        try:
            node_status_cmd = (
                f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach node status --insecure --format=csv"
            )
            node_status_output = kubectl.exec_command(node_status_cmd)

            # Count number of node lines (excluding header)
            lines = node_status_output.strip().split("\n")
            # First line is header, rest are nodes
            num_nodes = len([line for line in lines if line and not line.startswith("id,")])

            if num_nodes < expected_nodes:
                issue = f"Expected {expected_nodes} nodes, but only {num_nodes} found in cluster"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            elif num_nodes > expected_nodes:
                issue = f"Expected {expected_nodes} nodes, but {num_nodes} found in cluster"
                print(f"  ⚠️  {issue}")
                results["issues"].append(issue)
            else:
                print(f"  ✅ All {expected_nodes} nodes are present in cluster")

        except Exception as e:
            issue = f"Failed to verify cluster membership: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Step 3: Test SQL connectivity
        print(f"\n[3/3] Testing SQL connectivity...")
        try:
            sql_test_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT 1;'"
            sql_output = kubectl.exec_command(sql_test_cmd)

            # Check for successful query execution
            if "ERROR" in sql_output or "error" in sql_output.lower():
                issue = f"SQL query failed: {sql_output}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ SQL queries working correctly")

        except Exception as e:
            issue = f"Failed to test SQL connectivity: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Step 4: Verify all pods are running and ready
        print(f"\n[4/4] Checking pod status...")
        try:
            pods = kubectl.core_v1_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name=cockroachdb,app.kubernetes.io/instance={cluster_name}",
            )

            if len(pods.items) != expected_nodes:
                issue = f"Expected {expected_nodes} pods, found {len(pods.items)}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                # Check if all pods are running and ready
                not_ready = []
                for pod in pods.items:
                    if pod.status.phase != "Running":
                        not_ready.append(f"{pod.metadata.name} (phase: {pod.status.phase})")
                    elif pod.status.container_statuses:
                        for container in pod.status.container_statuses:
                            if not container.ready:
                                not_ready.append(f"{pod.metadata.name} (container not ready)")

                if not_ready:
                    issue = f"Pods not ready: {', '.join(not_ready)}"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)
                    results["success"] = False
                else:
                    print(f"  ✅ All {expected_nodes} pods are Running and Ready")

        except ApiException as e:
            issue = f"Error checking pod status: {e.reason}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False
        except Exception as e:
            issue = f"Error checking pod status: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Final summary
        print(f"\n{'='*60}")
        if results["success"]:
            print("✅ Initialize successful: CockroachDB cluster is initialized and operational")
        else:
            print(f"❌ Initialize failed: {len(results['issues'])} issue(s) found")
            for i, issue in enumerate(results["issues"], 1):
                print(f"   {i}. {issue}")
        print(f"{'='*60}\n")

        return results
