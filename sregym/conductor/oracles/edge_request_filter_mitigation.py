from sregym.conductor.oracles.mitigation import MitigationOracle


class EdgeRequestFilterMitigationOracle(MitigationOracle):
    """Pass when the vulnerable frontend WAF regex is no longer active."""

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        print("== Edge Request Filter Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service
        bad_regex = self.problem.bad_regex

        self._wait_for_rollouts(kubectl, namespace)

        try:
            deployment = kubectl.get_deployment(deployment_name, namespace)
        except Exception as e:
            print(f"❌ Failed to get deployment {deployment_name}: {e}")
            return {"success": False}

        container = None
        for candidate in deployment.spec.template.spec.containers:
            if candidate.name == deployment_name:
                container = candidate
                break

        if container is None:
            print(f"❌ Container {deployment_name} not found in deployment {deployment_name}")
            return {"success": False}

        env = {item.name: item.value for item in container.env or []}
        rule_enabled = env.get("WAF_RULE_ENABLED", "true").lower() != "false"
        vulnerable_rule_active = env.get("WAF_RULE_REGEX") == bad_regex and rule_enabled

        if vulnerable_rule_active:
            print(f"❌ Vulnerable WAF regex is still active: WAF_RULE_REGEX={bad_regex}")
            return {"success": False}

        pod_list = kubectl.list_pods(namespace)
        frontend_pods = [
            pod
            for pod in pod_list.items
            if pod.metadata.labels
            and (
                pod.metadata.labels.get("app.kubernetes.io/component") == deployment_name
                or pod.metadata.labels.get("app.kubernetes.io/name") == deployment_name
                or pod.metadata.labels.get("opentelemetry.io/name") == deployment_name
            )
        ]

        if not frontend_pods:
            print(f"❌ No pods found for deployment {deployment_name}")
            return {"success": False}

        for pod in frontend_pods:
            if pod.status.phase != "Running":
                print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                return {"success": False}
            for status in pod.status.container_statuses or []:
                if status.name == deployment_name and not status.ready:
                    print(f"❌ Container {status.name} in pod {pod.metadata.name} is not ready")
                    return {"success": False}

        print("✅ Vulnerable WAF regex is disabled, replaced, or rolled back; frontend pods are ready")
        return {"success": True}
