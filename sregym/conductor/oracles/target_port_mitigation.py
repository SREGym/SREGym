from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.utils import is_exact_match


class TargetPortMisconfigMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        faulty_service = self.problem.faulty_service
        results = {}

        # Check if target port has been reset to 9090
        configs = kubectl.get_service_json(faulty_service, namespace)
        target_port = configs["spec"]["ports"][0]["targetPort"]
        all_normal = is_exact_match(target_port, 9090)

        if all_normal:
            # Check if all services (not only faulty service) is back to normal (Running)
            pod_list = kubectl.list_pods(namespace)
            for pod in pod_list.items:
                if pod.status.container_statuses:
                    # Check container statuses
                    for container_status in pod.status.container_statuses:
                        if (
                            container_status.state.waiting
                            and container_status.state.waiting.reason == "CrashLoopBackOff"
                        ):
                            print(f"Container {container_status.name} is in CrashLoopBackOff")
                            all_normal = False
                        elif (
                            container_status.state.terminated
                            and container_status.state.terminated.reason != "Completed"
                        ):
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
