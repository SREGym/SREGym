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
        results = {}

        flag_ok = True
        pods_ok = True

        # Check 1: flag must be reverted in ConfigMap
        try:
            get_cm_command = (
                f"kubectl get configmap {self.configmap_name} -n {namespace} -o jsonpath='{{.data.{self.flag_key}}}'"
            )
            flag_value = kubectl.exec_command(get_cm_command).strip()

            if flag_value == self.flag_safe_value:
                print(f"✅ Flag {self.flag_key}={flag_value} is safe")
            else:
                print(f"❌ Flag {self.flag_key}={flag_value} is still active")
                flag_ok = False
        except Exception as e:
            print(f"❌ Failed to read ConfigMap {self.configmap_name}: {e}")
            flag_ok = False

        # Check 2: all pods must be Running and ready — runs regardless of Check 1
        try:
            pod_list = kubectl.list_pods(namespace)
            for pod in pod_list.items:
                if pod.status.phase != "Running":
                    print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                    pods_ok = False
                    break

                for container_status in pod.status.container_statuses:
                    if container_status.state.waiting and container_status.state.waiting.reason:
                        print(
                            f"❌ Container {container_status.name} is waiting: {container_status.state.waiting.reason}"
                        )
                        pods_ok = False
                    elif not container_status.ready:
                        print(f"⚠️ Container {container_status.name} is not ready")
                        pods_ok = False

                if not pods_ok:
                    break

            if pods_ok:
                print("✅ All pods are Running and ready")

        except Exception as e:
            print(f"❌ Failed to list pods: {e}")
            pods_ok = False

        results["success"] = flag_ok and pods_ok
        print(f"Mitigation Result: {'✅ Pass' if results['success'] else '❌ Fail'}")
        return results
