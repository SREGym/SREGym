import json
import yaml
import tempfile
from sregym.conductor.oracles.base import Oracle

class NonExistentStorageClassMitigationOracle(Oracle):
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
        ns = self.namespace
        name = self.cr_name
        results = {}
        evaluatePods = self.evaluatePods()
        print(f"Pod readiness: {evaluatePods}")

        cr = json.loads(self.kubectl.exec_command(
            f"kubectl get tidbcluster {name} -n tidb-cluster -o json"
        ))
        pd_sc   = (cr.get("spec", {}).get("pd", {})   or {}).get("storageClassName")
        tikv_sc = (cr.get("spec", {}).get("tikv", {}) or {}).get("storageClassName")

        pvc_pd_json = json.loads(self.kubectl.exec_command(
            f"kubectl get pvc -n {ns} "
            f"-l app.kubernetes.io/instance={name},app.kubernetes.io/component=pd -o json"
        ))
        pvc_tikv_json = json.loads(self.kubectl.exec_command(
            f"kubectl get pvc -n {ns} "
            f"-l app.kubernetes.io/instance={name},app.kubernetes.io/component=tikv -o json"
        ))

        def summarize_pvcs(pvc_list):
            out = []
            for p in pvc_list.get("items", []):
                meta = p.get("metadata", {}) or {}
                spec = p.get("spec", {}) or {}
                stat = p.get("status", {}) or {}
                out.append({
                    "name": meta.get("name"),
                    "storageClassName": spec.get("storageClassName"),
                    "phase": stat.get("phase"),  
                })
            return out

        pvc_pd   = summarize_pvcs(pvc_pd_json)
        pvc_tikv = summarize_pvcs(pvc_tikv_json)

        events_tail = self.kubectl.exec_command(
            f"kubectl get events -n {ns} --sort-by=.metadata.creationTimestamp | tail -n 50"
        )

        BAD = "ThisIsAStorageClass"
        cr_has_bad = (pd_sc == BAD) or (tikv_sc == BAD)
        pvc_shows_bad = any(e.get("storageClassName") == BAD for e in pvc_pd + pvc_tikv)
        any_pending = any(e.get("phase") == "Pending" for e in pvc_pd + pvc_tikv)
        print (f"cr_has_bad: {cr_has_bad}, pvc_shows_bad: {pvc_shows_bad}, any_pending: {any_pending}")

        fault_applied = cr_has_bad or pvc_shows_bad
        success = not fault_applied  

        return {
            "success": success,
            "cr_values": {"pd.storageClassName": pd_sc, "tikv.storageClassName": tikv_sc},
            "pvc_pd": pvc_pd,
            "pvc_tikv": pvc_tikv,
            "any_pvc_pending": any_pending,
            "events_tail": events_tail[-2000:],
            "fault_applied": fault_applied
        }
 