from sregym.conductor.oracles.base import Oracle


class ResourceQuotaExhaustionMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        quota_name = self.problem.quota_name
        results = {}

        # 1. Check if ResourceQuota still exists
        quota_exists = False
        quota_has_headroom = False
        quotas = kubectl.get_resource_quotas(namespace)

        for quota in quotas:
            if quota.metadata.name == quota_name:
                quota_exists = True
                # Check if it has pod limit and if there's headroom
                if "pods" in quota.spec.hard:
                    hard_limit = int(quota.spec.hard["pods"])
                    # Get current pod count
                    pod_list = kubectl.list_pods(namespace)
                    running_pods = [pod for pod in pod_list.items if pod.status.phase == "Running"]
                    current_pod_count = len(running_pods)

                    if hard_limit > current_pod_count:
                        quota_has_headroom = True
                        print(f"✅ ResourceQuota '{quota_name}' has headroom: {hard_limit} > {current_pod_count}")
                    else:
                        print(f"❌ ResourceQuota '{quota_name}' has no headroom: {hard_limit} <= {current_pod_count}")
                break

        if not quota_exists:
            print(f"❌ ResourceQuota '{quota_name}' does not exist (should be modified, not deleted)")
            results["success"] = False
            return results

        # 2. Check if all pods in namespace are Running (no Pending pods)
        pod_list = kubectl.list_pods(namespace)
        all_pods_running = True

        for pod in pod_list.items:
            if pod.status.phase == "Pending":
                print(f"❌ Pod {pod.metadata.name} is in Pending state")
                all_pods_running = False
            elif pod.status.phase != "Running":
                print(f"⚠️ Pod {pod.metadata.name} is in {pod.status.phase} state")

        if all_pods_running:
            print("✅ All pods in namespace are Running (no Pending pods)")

        # Success if: ResourceQuota exists, has headroom, and all pods are running
        success = quota_exists and quota_has_headroom and all_pods_running
        results["success"] = success

        print(f"Mitigation Result: {'✅ Pass' if success else '❌ Fail'}")
        return results
