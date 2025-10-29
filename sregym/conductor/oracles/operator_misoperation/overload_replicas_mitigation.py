import json
import yaml
import tempfile
from sregym.conductor.oracles.base import Oracle

class OverloadReplicasMitigationOracle(Oracle):
    def __init__(self, problem, deployment_name: str):
        super().__init__(problem)
        self.cr_name = "basic"
        self.deployment_name = deployment_name
        self.namespace = "tidb-cluster"
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
        results = {}
        evaluatePods = self.evaluatePods()
        print(f"Pod readiness: {evaluatePods}")
    
        cr = json.loads(self.kubectl.exec_command(
            f"kubectl get tidbcluster {name} -n tidb-cluster -o json"
        ))
        desired = (cr.get("spec", {}).get("tidb", {}) or {}).get("replicas")

        sts_name = f"{name}-tidb"
        try:
            sts = json.loads(self.kubectl.exec_command(
                f"kubectl get sts {sts_name} -n {ns} -o json"
            ))
            sts_replicas   = (sts.get("spec", {}) or {}).get("replicas")
            sts_ready      = (sts.get("status", {}) or {}).get("readyReplicas")
            sts_current    = (sts.get("status", {}) or {}).get("replicas")
        except Exception:
            sts = {}
            sts_replicas = sts_ready = sts_current = None

        try:
            pods = json.loads(self.kubectl.exec_command(
                f"kubectl get pods -n {ns} "
                f"-l app.kubernetes.io/instance={name},app.kubernetes.io/component=tidb -o json"
            ))
            pod_count = len(pods.get("items", []))
        except Exception:
            pod_count = None

        fault_applied = (desired == 100000)
        print("== Evaluation Result ===")
        print(f"CR desired replicas: {desired}")
        print(f"StatefulSet replicas: {sts_replicas}")
        print(f"StatefulSet current replicas: {sts_current}")
        print(f"StatefulSet ready replicas: {sts_ready}")
        print(f"TiDB pod count: {pod_count}")
        print(f"Fault applied: {fault_applied}")
        results["success"] = not fault_applied

        return results


       

 