import json

from sregym.conductor.oracles.base import Oracle
from sregym.service.kubectl import KubeCtl

INJECTED_SIDECAR_NAME = "otel-collector-sidecar"


class NativeSidecarMitigationOracle(Oracle):
    def __init__(self, problem, deployment_name: str):
        super().__init__(problem=problem)
        self.deployment_name = deployment_name
        self.namespace = problem.namespace
        self.kubectl = KubeCtl()

    def _all_pods_running(self) -> bool:
        result = self.kubectl.exec_command(f"kubectl get pods -n {self.namespace} -o json")
        try:
            pods = json.loads(result).get("items", [])
            return bool(pods) and all(p["status"]["phase"] == "Running" for p in pods)
        except (json.JSONDecodeError, KeyError):
            return False

    def _sidecar_removed(self) -> bool:
        result = self.kubectl.exec_command(f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o json")
        try:
            init_containers = (
                json.loads(result).get("spec", {}).get("template", {}).get("spec", {}).get("initContainers", [])
            )
            return not any(c.get("name") == INJECTED_SIDECAR_NAME for c in init_containers)
        except (json.JSONDecodeError, KeyError):
            return False

    def evaluate(self) -> dict:
        pods_ok = self._all_pods_running()
        sc_ok = self._sidecar_removed()

        success = pods_ok and sc_ok

        if not pods_ok:
            print("[Oracle] FAIL — not all pods are Running.")
        if not sc_ok:
            print(
                f"[Oracle] FAIL — deployment/{self.deployment_name} "
                f"still contains the '{INJECTED_SIDECAR_NAME}' "
                f"native sidecar in its spec."
            )
        return {"success": success}
