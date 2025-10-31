from sregym.conductor.oracles.base import Oracle


class MitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        pod_list = kubectl.list_pods(namespace)
        all_normal = True

        for pod in pod_list.items:
            if pod.status.phase != "Running":
                print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                all_normal = False
                break

            for container_status in pod.status.container_statuses:
                if container_status.state.waiting and container_status.state.waiting.reason:
                    print(f"❌ Container {container_status.name} is waiting: {container_status.state.waiting.reason}")
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

        results["success"] = all_normal
        return results
