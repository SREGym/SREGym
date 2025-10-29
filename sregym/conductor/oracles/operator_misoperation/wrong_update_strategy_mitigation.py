import json
import yaml
import tempfile
from sregym.conductor.oracles.base import Oracle

class WrongUpdateStrategyMitigationOracle(Oracle):
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
        


    def evaluate(self) -> dict:
        ns = self.namespace
        name = "basic"

        cr = json.loads(self.kubectl.exec_command(
            f"kubectl get tidbcluster {name} -n tidb-cluster -o json"
        ))
        cr_strategy = (
            cr.get("spec", {})
              .get("tidb", {})
              .get("statefulSetUpdateStrategy")
        )

        sts_name = f"{name}-tidb"
        sts_type = None
        try:
            sts = json.loads(self.kubectl.exec_command(
                f"kubectl get sts {sts_name} -n {ns} -o json"
            ))
            sts_type = (
                sts.get("spec", {})
                   .get("updateStrategy", {})
                   .get("type")
            )
        except Exception:
            pass

        BAD = "SomeStrategyForUpdate"
        fault_applied = (cr_strategy == BAD)
        print(f"cr_strategy: {cr_strategy}, sts_type: {sts_type}, fault_applied: {fault_applied}")

        return {
            "success": not fault_applied,
            "cr_statefulSetUpdateStrategy": cr_strategy,
            "sts_updateStrategy_type": sts_type,   
            "fault_applied": fault_applied
        }