from sregym.conductor.oracles.base import Oracle


class NamespaceMemoryLimitMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service
        results = {}

        # 1. Check if memory-based ResourceQuota is gone
        memory_quota_removed = True
        quotas = kubectl.get_resource_quotas(namespace)
        for quota in quotas:
            if "memory" in quota.spec.hard:
                print(f"❌ Memory quota still present: {quota.metadata.name}")
                memory_quota_removed = False

        # 2. Check if pod for the deployment is Running and Ready
        pod_list = kubectl.list_pods(namespace)
        pod_recovered = False

        for pod in pod_list.items:
            if deployment_name in pod.metadata.name and pod.status.phase == "Running":
                all_ready = all(container.ready for container in pod.status.container_statuses)
                if all_ready:
                    pod_recovered = True
                    break
                else:
                    print(f"⚠️ Pod {pod.metadata.name} is Running but containers not ready")
            elif deployment_name in pod.metadata.name:
                print(f"❌ Pod {pod.metadata.name} is not in Running phase: {pod.status.phase}")

        success = memory_quota_removed and pod_recovered
        results["success"] = success

        print(f"Mitigation Result: {'✅ Pass' if success else '❌ Fail'}")
        return results
