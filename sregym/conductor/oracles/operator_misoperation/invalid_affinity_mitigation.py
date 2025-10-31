import json
import yaml
import tempfile
from sregym.conductor.oracles.base import Oracle

class InvalidAffinityMitigationOracle(Oracle):
    def __init__(self, problem, deployment_name: str):
        super().__init__(problem)
        self.cr_name = "basic"

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
        evaluatePods = self.evaluatePods()
        print(f"Pod readiness: {evaluatePods}")
        ns = self.namespace
        name = "basic"  

        cr_json = json.loads(self.kubectl.exec_command(
            f"kubectl get tidbcluster {name} -n tidb-cluster -o json"
        ))
        cr_effects = [
            t.get("effect")
            for t in (cr_json.get("spec", {}).get("tidb", {}).get("tolerations", []) or [])
            if isinstance(t, dict)
        ]


        try:
            sts_json = json.loads(self.kubectl.exec_command(
                f"kubectl get sts {name}-tidb -n {ns} -o json"
            ))
            tpl_tolerations = (sts_json.get("spec", {})
                                        .get("template", {})
                                        .get("spec", {})
                                        .get("tolerations", []) or [])
            sts_effects = [t.get("effect") for t in tpl_tolerations if isinstance(t, dict)]
        except Exception:
            sts_json = {}
            sts_effects = []


        pod_effects = []
        try:
            pods_json = json.loads(self.kubectl.exec_command(
                f"kubectl get pods -n {ns} -l app.kubernetes.io/instance={name},app.kubernetes.io/component=tidb -o json"
            ))
            for item in pods_json.get("items", []):
                tol = (item.get("spec", {}) or {}).get("tolerations", []) or []
                pod_effects.extend([t.get("effect") for t in tol if isinstance(t, dict)])
        except Exception:
            pods_json = {}

        try:
            ev = self.kubectl.exec_command(f"kubectl get events -n {ns} --sort-by=.metadata.creationTimestamp | tail -n 30")
        except Exception:
            ev = ""

        bad = "TAKE_SOME_EFFECT"
        applied_in = {
            "cr_has_bad_effect": (bad in cr_effects),
            "sts_has_bad_effect": (bad in sts_effects),
            "any_pod_has_bad_effect": (bad in pod_effects),
        }
        success = not any(applied_in.values())

        return {
            "success": success,
            "details": {
                "cr_effects": cr_effects,
                "sts_effects": sts_effects,
                "pod_effects": pod_effects,
                "recent_events_tail": ev[-2000:],  
            },
            "applied_in": applied_in
        }




        

    