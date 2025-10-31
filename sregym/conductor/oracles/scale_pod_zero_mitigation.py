from sregym.conductor.oracles.base import Oracle


class ScalePodZeroMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        faulty_service = self.problem.faulty_service
        results = {}

        all_normal = True

        deployment = kubectl.get_deployment(faulty_service, namespace)

        if deployment is None:
            print(f"Deployment for {faulty_service} not found")
            all_normal = False
        else:
            desired_replicas = deployment.spec.replicas
            available_replicas = deployment.status.available_replicas

            if desired_replicas != 1 or available_replicas != 1:
                print(
                    f"Faulty service {faulty_service} has {available_replicas} available replicas out of {desired_replicas} desired"
                )
                all_normal = False

        # Check if all services are running normally
        pod_list = kubectl.list_pods(namespace)
        for pod in pod_list.items:
            for container_status in pod.status.container_statuses:
                if container_status.state.waiting and container_status.state.waiting.reason == "CrashLoopBackOff":
                    print(f"Container {container_status.name} is in CrashLoopBackOff")
                    all_normal = False
                elif container_status.state.terminated and container_status.state.terminated.reason != "Completed":
                    print(
                        f"Container {container_status.name} is terminated with reason: {container_status.state.terminated.reason}"
                    )
                    all_normal = False
                elif not container_status.ready:
                    print(f"Container {container_status.name} is not ready")
                    all_normal = False

            if not all_normal:
                break

        results["success"] = all_normal

        print(f"Mitigation Result: {'Pass ✅' if all_normal else 'Fail ❌'}")

        return results
