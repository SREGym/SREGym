import json
import yaml
import tempfile
from srearena.conductor.oracles.base import Oracle

class NonExistentStorageMitigationOracle(Oracle):
    def __init__(self, problem, deployment_name: str):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.namespace = problem.namespace
        self.kubectl = problem.kubectl

    def evaluatePods(self) -> dict:
        print("== Evaluating pod readiness ==")
        try:
            output = self.kubectl.exec_command(
                f"kubectl get pods -n {self.namespace} -o yaml"
            )
            pods = yaml.safe_load(output)
            pods_list = pods.get("items", [])
            pod_statuses = {}
            for pod in pods_list:
                pod_name = pod["metadata"]["name"]
                container_status = pod["status"].get("containerStatuses", [])
                if container_status:
                    state = container_status[0].get("state", {})
                    if "waiting" in state:
                        reason = state["waiting"].get("reason", "Unknown")
                        pod_statuses[pod_name] = reason
                    elif "running" in state:
                        pod_statuses[pod_name] = "Running"
                    else:
                        pod_statuses[pod_name] = "Terminated"
                else:
                    pod_statuses[pod_name] = "No Status"

            print("Pod Statuses:")
            for pod, status in pod_statuses.items():
                print(f" - {pod}: {status}")
                if status != "Running":
                        print(f"Pod {pod} is not running. Status: {status}")
                        return {"success": False}
            print("All pods are running.")
            return {"success": True}
        except Exception as e:
            print(f"Error during evaluation: {str(e)}")
            return {"success": False}
        


    def getTheValue(self) -> dict:
        output = self.kubectl.exec_command(
               f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o yaml"
              )
        deployment = yaml.safe_load(output)
        pd = deployment["spec"].get("pd")
        storage = deployment["tidb"].get("podSecurity")
        if (storage == "ThisIsAStorageClass"):
            return {"success": False}


       

 