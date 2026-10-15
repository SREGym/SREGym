from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.utils import is_exact_match


class TargetPortMisconfigMitigationOracle(Oracle):
    importance = 1.0

    def __init__(self, problem, *, valid_target_ports: tuple[int | str, ...] = (9090,)):
        super().__init__(problem)
        if not valid_target_ports:
            raise ValueError("At least one valid target port is required")
        self.valid_target_ports = valid_target_ports

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        faulty_service = self.problem.faulty_service
        results = {}

        # Validate the real Service response against the application's ports.
        configs = kubectl.get_service_json(faulty_service, namespace)
        target_port = configs["spec"]["ports"][0]["targetPort"]
        all_normal = any(is_exact_match(target_port, value) for value in self.valid_target_ports)

        if all_normal:
            # Check if all services (not only faulty service) is back to normal (Running)
            pod_list = kubectl.list_pods(namespace)
            for pod in pod_list.items:
                # One-shot Job pods (an app's migrate/seed Jobs) end Succeeded by design.
                if self.is_completed_job_pod(pod):
                    continue
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
