from sregym.conductor.oracles.base import Oracle


class UnitMismatchEnvMitigationOracle(Oracle):
    """Mitigation is complete when the target env var holds its correct value and
    pods in the namespace are Running + Ready."""

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        faulty_service = self.problem.faulty_service
        env_var = self.problem.env_var
        correct_value = self.problem.correct_value
        results = {}

        env_ok = False
        try:
            deployment = kubectl.get_deployment(faulty_service, namespace)
            for container in deployment.spec.template.spec.containers:
                if not getattr(container, "env", None):
                    continue
                for env in container.env:
                    if env.name == env_var and env.value == correct_value:
                        print(
                            f"✅ Found {env_var}={correct_value} in container {container.name}"
                        )
                        env_ok = True
                        break
                if env_ok:
                    break
            if not env_ok:
                print(
                    f"❌ {env_var} is not set to the correct value '{correct_value}' in deployment {faulty_service}"
                )
        except Exception as e:
            print(f"❌ Failed to get deployment {faulty_service}: {e}")

        pods_ok = True
        if env_ok:
            pod_list = kubectl.list_pods(namespace)
            for pod in pod_list.items:
                if pod.status.phase != "Running":
                    print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                    pods_ok = False
                    break
                for cs in pod.status.container_statuses or []:
                    if cs.state.waiting and cs.state.waiting.reason:
                        print(
                            f"❌ Container {cs.name} is waiting: {cs.state.waiting.reason}"
                        )
                        pods_ok = False
                    elif (
                        cs.state.terminated
                        and cs.state.terminated.reason != "Completed"
                    ):
                        print(
                            f"❌ Container {cs.name} terminated: {cs.state.terminated.reason}"
                        )
                        pods_ok = False
                    elif not cs.ready:
                        print(f"⚠️ Container {cs.name} is not ready")
                        pods_ok = False
                if not pods_ok:
                    break

        success = env_ok and pods_ok
        results["success"] = success
        print(f"Mitigation Result: {'✅ Pass' if success else '❌ Fail'}")
        return results
