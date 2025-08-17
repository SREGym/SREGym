import os

from clients.stratus.weak_oracles.base_oracle import BaseOracle, OracleResult


class ClusterStateOracle(BaseOracle):
    async def validate(self, namespace="default", **kwargs) -> OracleResult:
        """
        Validates the Kubernetes cluster status.

        Args:
            namespace (str): The namespace to check

        Returns:
            dict: A dict containing validation results with 'success' and 'issues' keys
        """
        results = {"success": True, "issues": []}

        from kubernetes import client, config

        # Load Kubernetes configuration
        if os.path.exists(os.path.expanduser("~/.kube/config")):
            config.load_kube_config()
        else:
            config.load_incluster_config()

        # print(f"Validating cluster status on namespace '{namespace}'...")

        try:
            # Initialize Kubernetes API client
            v1 = client.CoreV1Api()

            # Get all pods in the namespace
            pod_list = v1.list_namespaced_pod(namespace)

            for pod in pod_list.items:
                pod_name = pod.metadata.name
                pod_issues = []

                # Skip if pod is being terminated
                if pod.metadata.deletion_timestamp:
                    continue

                # Check pod status
                if pod.status.phase not in ["Running", "Succeeded"]:
                    issue = f"Pod {pod_name} is in {pod.status.phase} state"
                    pod_issues.append(issue)
                    results["issues"].append(issue)
                    results["success"] = False

                # Check container statuses
                if pod.status.container_statuses:
                    for container_status in pod.status.container_statuses:
                        container_name = container_status.name

                        if container_status.state.waiting:
                            reason = container_status.state.waiting.reason
                            issue = f"Container {container_name} in pod {pod_name} is waiting: {reason}"
                            if reason == "CrashLoopBackOff":
                                issue = f"Container {container_name} is in CrashLoopBackOff"
                            pod_issues.append(issue)
                            results["issues"].append(issue)
                            results["success"] = False

                        elif (
                            container_status.state.terminated
                            and container_status.state.terminated.reason != "Completed"
                        ):
                            reason = container_status.state.terminated.reason
                            issue = f"Container {container_name} is terminated with reason: {reason}"
                            pod_issues.append(issue)
                            results["issues"].append(issue)
                            results["success"] = False

                        elif not container_status.ready and pod.status.phase == "Running":
                            issue = f"Container {container_name} is not ready"
                            pod_issues.append(issue)
                            results["issues"].append(issue)
                            results["success"] = False

                if pod_issues:
                    print(f"Issues found with pod {pod_name}:")
                    for issue in pod_issues:
                        print(f"  - {issue}")

            if results["success"]:
                print("All pods are running normally.")
            else:
                print(f"Found {len(results['issues'])} issues in the cluster.")

        except Exception as e:
            results["success"] = False
            results["issues"].append(f"Error validating cluster: {str(e)}")
            print(f"Error validating cluster: {str(e)}")

        results = OracleResult(success=results["success"], issues=results["issues"])

        return results
