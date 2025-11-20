"""Oracle for validating CockroachDB cluster upscaling from 3 to 5 replicas."""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBUpscaleOracle(Oracle):
    """
    Oracle that validates successful upscaling of a CockroachDB cluster.

    This oracle verifies that:
    1. The CrdbCluster custom resource has the correct node count (5)
    2. The StatefulSet has the correct replica count (5)
    3. All 5 replicas are ready and running
    4. All pods are in Running phase with containers ready
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether the CockroachDB cluster has been successfully scaled to 5 replicas.

        Returns:
            dict: Results dictionary with 'success' (bool) and 'issues' (list) keys
        """
        print("== CockroachDB Upscale Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {"success": True, "issues": []}

        expected_replicas = 5

        # Step 1: Check CrdbCluster custom resource spec.nodes field
        print(f"\n[1/4] Checking CrdbCluster CR spec.nodes...")
        try:
            from kubernetes import dynamic
            from kubernetes.client import api_client

            dyn_client = dynamic.DynamicClient(
                api_client.ApiClient(configuration=kubectl.core_v1_api.api_client.configuration)
            )

            # Get the CrdbCluster CRD
            crdb_api = dyn_client.resources.get(api_version="crdb.cockroachlabs.com/v1alpha1", kind="CrdbCluster")

            # Fetch the cockroachdb cluster resource
            cr = crdb_api.get(name="cockroachdb", namespace=namespace)
            actual_nodes = cr.spec.get("nodes", 0)

            if actual_nodes != expected_replicas:
                issue = f"CrdbCluster spec.nodes is {actual_nodes}, expected {expected_replicas}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ CrdbCluster spec.nodes correctly set to {expected_replicas}")

        except ApiException as e:
            issue = f"Failed to get CrdbCluster CR: {e.reason}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False
            return results
        except Exception as e:
            issue = f"Error accessing CrdbCluster CR: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False
            return results

        # Step 2: Check StatefulSet replica count and readiness
        print(f"\n[2/4] Checking StatefulSet replicas...")
        try:
            # List all StatefulSets in the namespace
            sts_list = kubectl.apps_v1_api.list_namespaced_stateful_set(namespace)

            cockroachdb_sts = None
            for sts in sts_list.items:
                if "cockroachdb" in sts.metadata.name:
                    cockroachdb_sts = sts
                    break

            if not cockroachdb_sts:
                issue = "CockroachDB StatefulSet not found in namespace"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
                return results

            # Check spec.replicas
            actual_replicas = cockroachdb_sts.spec.replicas
            if actual_replicas != expected_replicas:
                issue = f"StatefulSet spec.replicas is {actual_replicas}, expected {expected_replicas}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ StatefulSet spec.replicas correctly set to {expected_replicas}")

            # Check status.readyReplicas
            ready_replicas = cockroachdb_sts.status.ready_replicas or 0
            if ready_replicas != expected_replicas:
                issue = f"Only {ready_replicas}/{expected_replicas} replicas are ready"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ All {expected_replicas} replicas are ready")

        except ApiException as e:
            issue = f"Failed to check StatefulSet: {e.reason}"
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

        # Step 3: Verify the correct number of CockroachDB pods exist
        print(f"\n[3/4] Checking pod count...")
        try:
            pod_list = kubectl.list_pods(namespace)
            cockroachdb_pods = [
                pod
                for pod in pod_list.items
                if "cockroachdb" in pod.metadata.name and not pod.metadata.name.endswith("-init")
            ]

            if len(cockroachdb_pods) != expected_replicas:
                issue = f"Found {len(cockroachdb_pods)} CockroachDB pods, expected {expected_replicas}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ Correct number of pods found: {expected_replicas}")

        except Exception as e:
            issue = f"Error listing pods: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False
            return results

        # Step 4: Check health of all CockroachDB pods
        print(f"\n[4/4] Checking pod and container health...")
        all_pods_healthy = True

        for pod in cockroachdb_pods:
            pod_name = pod.metadata.name

            # Skip pods that are being deleted
            if pod.metadata.deletion_timestamp:
                print(f"  ⏭️  Skipping {pod_name} (being deleted)")
                continue

            # Check pod phase
            if pod.status.phase not in ["Running", "Succeeded"]:
                issue = f"Pod {pod_name} is in {pod.status.phase} phase (expected Running)"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
                all_pods_healthy = False
                continue

            # Check container statuses
            if pod.status.container_statuses:
                for container_status in pod.status.container_statuses:
                    container_name = container_status.name

                    # Check if container is waiting
                    if container_status.state.waiting:
                        reason = container_status.state.waiting.reason
                        issue = f"Container {container_name} in pod {pod_name} is waiting: {reason}"
                        print(f"  ❌ {issue}")
                        results["issues"].append(issue)
                        results["success"] = False
                        all_pods_healthy = False

                    # Check if container is terminated (and not completed)
                    elif container_status.state.terminated:
                        reason = container_status.state.terminated.reason
                        if reason != "Completed":
                            issue = f"Container {container_name} in pod {pod_name} terminated: {reason}"
                            print(f"  ❌ {issue}")
                            results["issues"].append(issue)
                            results["success"] = False
                            all_pods_healthy = False

                    # Check if container is ready
                    elif not container_status.ready and pod.status.phase == "Running":
                        issue = f"Container {container_name} in pod {pod_name} is not ready"
                        print(f"  ❌ {issue}")
                        results["issues"].append(issue)
                        results["success"] = False
                        all_pods_healthy = False

        if all_pods_healthy:
            print(f"  ✅ All pods and containers are healthy")

        # Final summary
        print(f"\n{'='*60}")
        if results["success"]:
            print("✅ Mitigation successful: CockroachDB cluster scaled to 5 replicas")
        else:
            print(f"❌ Mitigation failed: {len(results['issues'])} issue(s) found")
            for i, issue in enumerate(results["issues"], 1):
                print(f"   {i}. {issue}")
        print(f"{'='*60}\n")

        return results
