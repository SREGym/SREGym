from srearena.conductor.oracles.base import Oracle


class AssignNonExistentNodeMitigationOracle(Oracle):
    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        faulty_service = self.problem.faulty_service
        results = {}

        # Check if all services (not only faulty service) is back to normal (Running)
        pod_list = kubectl.list_pods(namespace)
        all_normal = True

        # Check if the faulty service exists
        faulty_service_exists = any(pod.metadata.name.startswith(faulty_service) for pod in pod_list.items)
        if not faulty_service_exists:
            print(f"Pod named {faulty_service} does not exist.")
            all_normal = False
        else:
            for pod in pod_list.items:
                if pod.status.phase != "Running":
                    print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                    all_normal = False
                    break

                for container_status in pod.status.container_statuses:
                    if container_status.state.waiting and container_status.state.waiting.reason:
                        print(
                            f"❌ Container {container_status.name} is waiting: {container_status.state.waiting.reason}"
                        )
                        all_normal = False
                    elif container_status.state.terminated and container_status.state.terminated.reason != "Completed":
                        print(
                            f"❌ Container {container_status.name} terminated: {container_status.state.terminated.reason}"
                        )
                        all_normal = False
                    elif not container_status.ready:
                        print(f"⚠️ Container {container_status.name} is not ready")
                        all_normal = False

                if not all_normal:
                    break

        results["Mitigation Success"] = all_normal
        results["success"] = all_normal

        print(f"Mitigation Result: {'Pass ✅' if all_normal else 'Fail ❌'}")

        return results
