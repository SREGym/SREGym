from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.mitigation import MitigationOracle


class FinalizerDeadlockMitigationOracle(Oracle):
    importance = 1.0

    def __init__(self, problem, configmap_name: str, finalizer: str):
        super().__init__(problem)
        self.configmap_name = configmap_name
        self.finalizer = finalizer

    def evaluate(self) -> dict:
        print("== Finalizer Deadlock Mitigation Evaluation ==")

        namespace = self.problem.namespace
        core_v1 = self.problem.kubectl.core_v1_api

        try:
            cm = core_v1.read_namespaced_config_map(self.configmap_name, namespace)
        except ApiException as e:
            if e.status == 404:
                print(f"[OK] ConfigMap {self.configmap_name} has been fully deleted.")
                app_health = MitigationOracle(self.problem).evaluate()
                return {"success": bool(app_health.get("success"))}
            print(f"[FAIL] Failed to inspect ConfigMap {self.configmap_name}: {e}")
            return {"success": False}

        finalizers = cm.metadata.finalizers or []
        deletion_timestamp = cm.metadata.deletion_timestamp

        if deletion_timestamp and self.finalizer in finalizers:
            print(f"[FAIL] ConfigMap {self.configmap_name} is still stuck terminating with finalizer {self.finalizer}.")
            return {"success": False}

        if deletion_timestamp:
            print(f"[FAIL] ConfigMap {self.configmap_name} is still terminating.")
            return {"success": False}

        print(f"[FAIL] ConfigMap {self.configmap_name} still exists; expected cleanup to complete.")
        return {"success": False}
