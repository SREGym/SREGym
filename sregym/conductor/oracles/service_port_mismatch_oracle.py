from sregym.conductor.oracles.base import Oracle


class ServicePortMismatchMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Service Port Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        faulty_service = self.problem.faulty_service
        results = {}

        endpoints_ready = False
        try:
            endpoints = kubectl.core_v1_api.read_namespaced_endpoints(faulty_service, namespace)
            endpoints_ready = any(subset.addresses for subset in (endpoints.subsets or []))
            if not endpoints_ready:
                print(f"Service {faulty_service} has no ready endpoints")
        except Exception as exc:
            print(f"Error retrieving endpoints for service {faulty_service}: {exc}")

        pods_running = True
        for pod in kubectl.list_pods(namespace).items:
            if pod.status.phase != "Running":
                print(f"Pod {pod.metadata.name} is in phase {pod.status.phase}")
                pods_running = False
                break

            container_statuses = pod.status.container_statuses or []
            if not container_statuses or not all(container_status.ready for container_status in container_statuses):
                print(f"Pod {pod.metadata.name} is not fully ready")
                pods_running = False
                break

        results["success"] = endpoints_ready and pods_running

        print(f"Mitigation Result: {'Pass ✅' if results['success'] else 'Fail ❌'}")

        return results
