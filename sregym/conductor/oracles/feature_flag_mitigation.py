"""Mitigation oracle for feature flag latent bug problem."""

from sregym.conductor.oracles.base import Oracle


class FeatureFlagMitigationOracle(Oracle):
    def __init__(self, problem, configmap_name: str, flag_key: str, flag_safe_value: str):
        super().__init__(problem)
        self.configmap_name = configmap_name
        self.flag_key = flag_key
        self.flag_safe_value = flag_safe_value

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        all_normal = True
        results = {}

        # Check 1: flag must be reverted in ConfigMap
        try:
            get_cm_command = (
                f"kubectl get configmap {self.configmap_name} "
                f"-n {namespace} "
                f"-o jsonpath='{{.data.{self.flag_key}}}'"
            )
            flag_value = kubectl.exec_command(get_cm_command).strip()

            if flag_value == self.flag_safe_value:
                print(f"✅ Flag {self.flag_key}={flag_value} is safe")
            else:
                print(f"❌ Flag {self.flag_key}={flag_value} is still active")
                all_normal = False
        except Exception as e:
            print(f"❌ Failed to read ConfigMap {self.configmap_name}: {e}")
            all_normal = False

        # Check 2: all pods must be Running and ready
        if all_normal:
            try:
                pod_list = kubectl.list_pods(namespace)
                for pod in pod_list.items:
                    if pod.status.phase != "Running":
                        print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                        all_normal = False
                        break

                    for container_status in pod.status.container_statuses:
                        if container_status.state.waiting and container_status.state.waiting.reason:
                            print(
                                f"❌ Container {container_status.name} is waiting: "
                                f"{container_status.state.waiting.reason}"
                            )
                            all_normal = False
                        elif not container_status.ready:
                            print(f"⚠️ Container {container_status.name} is not ready")
                            all_normal = False

                    if not all_normal:
                        break

                if all_normal:
                    print("✅ All pods are Running and ready")

            except Exception as e:
                print(f"❌ Failed to list pods: {e}")
                all_normal = False

        results["success"] = all_normal
        print(f"Mitigation Result: {'✅ Pass' if results['success'] else '❌ Fail'}")
        return results