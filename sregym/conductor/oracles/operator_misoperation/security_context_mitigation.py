import json
import yaml
import tempfile
from sregym.conductor.oracles.base import Oracle

class SecurityContextMitigationOracle(Oracle):
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
        evaluatePods = self.evaluatePods()
        print(f"Pod Readiness: {evaluatePods}")

        cr = json.loads(self.kubectl.exec_command(
            f"kubectl get tidbcluster {name} -n tidb-cluster -o json"
        ))
        run_as_user = (
            cr.get("spec", {})
              .get("tidb", {})
              .get("podSecurityContext", {})
              .get("runAsUser")
        )

        sts_name = f"{name}-tidb"
        sts_run_as_user = None
        try:
            sts = json.loads(self.kubectl.exec_command(
                f"kubectl get sts {sts_name} -n {ns} -o json"
            ))
            sts_run_as_user = (
                sts.get("spec", {})
                   .get("template", {})
                   .get("spec", {})
                   .get("securityContext", {})
                   .get("runAsUser")
            )
        except Exception:
            pass

        pod_run_as_users = []
        try:
            pods = json.loads(self.kubectl.exec_command(
                f"kubectl get pods -n {ns} "
                f"-l app.kubernetes.io/instance={name},app.kubernetes.io/component=tidb -o json"
            ))
            for item in pods.get("items", []):
                pod_run_as_users.append(
                    (item.get("metadata", {}).get("name"),
                     (item.get("spec", {}).get("securityContext") or {}).get("runAsUser"))
                )
        except Exception:
            pass
        print("== Evaluation Result ===")
        print(f"CR runAsUser: {run_as_user}")
        print(f"StatefulSet runAsUser: {sts_run_as_user}")
        print(f"Pod runAsUsers: {pod_run_as_users}")
        print(f"Fault applied: {run_as_user == -1}")


        fault_present = (run_as_user == -1)
        return {
            "success": not fault_present,
            "cr_runAsUser": run_as_user,
            "sts_runAsUser": sts_run_as_user,
            "pod_runAsUsers": pod_run_as_users,
            "fault_applied": fault_present
        }
       

 