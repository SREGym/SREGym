from sregym.conductor.oracles.base import Oracle


class MissingEnvVariableMitigationOracle(Oracle):

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        faulty_service = self.problem.faulty_service
        env_var = self.problem.env_var
        env_var_value = self.problem.env_var_value

        all_normal = False
        # check if deployment exists
        try:
            deployment = kubectl.get_deployment(faulty_service, namespace)
            env_var_found = False

            # check if env var exists in deployment
            for container in deployment.spec.template.spec.containers:
                if hasattr(container, 'env') and container.env:
                    for env in container.env:
                        if env.name == env_var and env.value == env_var_value:
                            print(f"✅ Found environment variable {env_var}={env_var_value} in container {container.name}")
                            env_var_found = True
                            break
                    if env_var_found:
                        break
            
            if not env_var_found:
                print(f"❌ Failed to find environment variable {env_var}={env_var_value} in deployment {faulty_service}")

            all_normal = env_var_found

        except Exception as e:
            print(f"❌ Failed to get deployment {faulty_service}: {e}")
            all_normal = False
            
        if all_normal:
            pod_list = kubectl.list_pods(namespace)

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

        print(f"Mitigation Result: {'✅ Pass' if results['success'] else '❌ Fail'}")
        return results